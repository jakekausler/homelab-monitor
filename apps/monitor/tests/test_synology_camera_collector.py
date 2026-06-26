"""Unit tests for the synology_cameras collector (STAGE-008-015, fixture-based).

100% branch coverage of cameras.py. Field names + payload shapes are LIVE-VERIFIED
(captured JSON: cameras[] list of dicts each with newName/status/stream1.{fps,resolution};
SS.Info has cameraNumber/liscenseNumber [DSM TYPO]/maxCameraSupport/version object of
strings). Exercises the CO-EQUAL combine (ok=False ONLY when BOTH fetches fail), the
always-emit seeded rollups + info scalars, the emit-on-presence per-camera families + version
carrier, and every conditional guard's BOTH sides.
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
from homelab_monitor.plugins.collectors.integrations.synology.cameras import (
    M_CAMERA_CONNECTED,
    M_CAMERA_FPS,
    M_CAMERA_INFO,
    M_CAMERA_RECORDING_KEEP_DAYS,
    M_CAMERA_RECORDING_KEEP_SIZE_MB,
    M_CAMERA_RECORDING_RETENTION_MODE,
    M_CAMERA_RESOLUTION,
    M_CAMERA_RESOLUTION_PIXELS,
    M_CAMERA_STATUS,
    M_CAMERAS_CONNECTED_TOTAL,
    M_CAMERAS_DISCONNECTED_TOTAL,
    M_CAMERAS_TOTAL,
    M_INFO_CAMERA_NUMBER,
    M_INFO_LICENSE_MAX,
    M_INFO_LICENSE_USED,
    M_INFO_VERSION,
    SynologyCameraCollector,
)

_API_TOOK = "homelab_synology_api_took_seconds"
_DROP = "homelab_metric_family_dropped_series"

_EXPECTED_INTERVAL = 60.0
_EXPECTED_TIMEOUT = 30.0

# 16 cap-routed families emitted by _emit.
_FAMILY_COUNT = 16
# Two co-equal fetches: camera list + info.
_EXPECTED_API_TOOK_COUNT = 2

# Live fixture cardinalities / values.
_CAMERA_COUNT: int = 3
_LIVE_CAMERA_COUNT = 3.0
_LIVE_CONNECTED = 3.0
_LIVE_DISCONNECTED = 0.0
_LIVE_FPS = 15.0
_LIVE_KEEP_DAYS = 30.0
_LIVE_KEEP_SIZE_MB = 1000.0
_DRIVEWAY_PIXELS = 3840.0 * 2160.0  # 8294400.0
_LIVE_CAMERA_NUMBER = 3.0
_LIVE_LICENSE_USED = 3.0
_LIVE_LICENSE_MAX = 90.0
_LIVE_VERSION = "9.2.4-11880"
_STATUS_CONNECTED = 1.0
_STATUS_DISCONNECTED = 2.0


def _resp(payload: object, endpoint: str) -> SynologyResponse:
    return SynologyResponse(payload=payload, took_seconds=0.5, endpoint=endpoint)


def _cam_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.SurveillanceStation.Camera/List")


def _info_resp(payload: object) -> SynologyResponse:
    return _resp(payload, "SYNO.SurveillanceStation.Info/GetInfo")


def _camera(
    *,
    cam_id: int = 1,
    name: str = "Driveway",
    status: int = 1,
    resolution: str = "3840x2160",
    fps: int = 15,
) -> dict[str, object]:
    """One live-shaped camera record (overridable for edge fixtures)."""
    return {
        "id": cam_id,
        "newName": name,
        "model": "Generic_ONVIF",
        "vendor": "ONVIF",
        "ip": "192.168.2.103",
        "mac": "EC:71:DB:7C:58:87",
        "port": 8000,
        "status": status,
        "recordingKeepDays": 30,
        "enableRecordingKeepDays": False,
        "enableRecordingKeepSize": True,
        "recordingKeepSize": "1000",
        "stream1": {"fps": fps, "resolution": resolution},
    }


def _live_camera_payload() -> dict[str, object]:
    """The verified live shape: 3 connected ONVIF cameras."""
    return {
        "cameras": [
            _camera(cam_id=1, name="Driveway", resolution="3840x2160"),
            _camera(cam_id=2, name="Backyard", resolution="2560x1440"),
            _camera(cam_id=3, name="Doorbell", resolution="1920x2560"),
        ]
    }


def _live_info_payload() -> dict[str, object]:
    """The verified live SS.Info shape (liscenseNumber DSM typo verbatim)."""
    return {
        "cameraNumber": 3,
        "liscenseNumber": 3,
        "maxCameraSupport": 90,
        "version": {"build": "11880", "major": "9", "minor": "2", "small": "4"},
    }


class _FakeSynology:
    """Stand-in for ctx.synology with 2 independently programmable methods."""

    def __init__(self, cameras: object = None, info: object = None) -> None:
        self._cameras = cameras if cameras is not None else _cam_resp(_live_camera_payload())
        self._info = info if info is not None else _info_resp(_live_info_payload())

    async def ss_camera_list(self) -> object:
        return self._cameras

    async def ss_info(self) -> object:
        return self._info


@dataclass
class _Ctx:
    """Typed stand-in for CollectorContext used in camera tests."""

    vm: MemoryRetainingMetricsWriter = field(default_factory=MemoryRetainingMetricsWriter)
    synology: object = None


def _ctx(writer: MemoryRetainingMetricsWriter, synology: object) -> _Ctx:
    return _Ctx(vm=writer, synology=synology)


def _gauges_named(
    writer: MemoryRetainingMetricsWriter, name: str
) -> list[tuple[str, float, dict[str, str]]]:
    return [g for g in writer.gauges if g[0] == name]


# === Test cases: classvars + rows 1-37 ===


def test_camera_classvars() -> None:
    assert SynologyCameraCollector.name == "synology_cameras"
    assert SynologyCameraCollector.interval == timedelta(seconds=60)
    assert SynologyCameraCollector.interval.total_seconds() == _EXPECTED_INTERVAL
    assert SynologyCameraCollector.timeout == timedelta(seconds=30)
    assert SynologyCameraCollector.timeout.total_seconds() == _EXPECTED_TIMEOUT
    assert SynologyCameraCollector.concurrency_group == "synology"


@pytest.mark.asyncio
async def test_camera_full_live_shape() -> None:
    """Row 1: Full live shape (success path, 3 cameras, all families emit)."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology()))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    gauges_total = _gauges_named(writer, M_CAMERAS_TOTAL)
    assert len(gauges_total) == 1
    assert gauges_total[0][1] == _LIVE_CAMERA_COUNT

    gauges_connected = _gauges_named(writer, M_CAMERAS_CONNECTED_TOTAL)
    assert len(gauges_connected) == 1
    assert gauges_connected[0][1] == _LIVE_CONNECTED

    gauges_disconnected = _gauges_named(writer, M_CAMERAS_DISCONNECTED_TOTAL)
    assert len(gauges_disconnected) == 1
    assert gauges_disconnected[0][1] == _LIVE_DISCONNECTED

    connected_gauges = _gauges_named(writer, M_CAMERA_CONNECTED)
    assert len(connected_gauges) == _CAMERA_COUNT
    assert all(g[1] == 1.0 for g in connected_gauges)

    info_gauges = _gauges_named(writer, M_CAMERA_INFO)
    assert len(info_gauges) == _CAMERA_COUNT
    assert any(g[2].get("model") == "Generic_ONVIF" for g in info_gauges)

    fps_gauges = _gauges_named(writer, M_CAMERA_FPS)
    assert len(fps_gauges) == _CAMERA_COUNT
    assert all(g[1] == _LIVE_FPS for g in fps_gauges)

    res_pix_gauges = _gauges_named(writer, M_CAMERA_RESOLUTION_PIXELS)
    assert len(res_pix_gauges) == _CAMERA_COUNT
    assert any(g[1] == _DRIVEWAY_PIXELS for g in res_pix_gauges)

    res_gauges = _gauges_named(writer, M_CAMERA_RESOLUTION)
    assert len(res_gauges) == _CAMERA_COUNT

    keep_days_gauges = _gauges_named(writer, M_CAMERA_RECORDING_KEEP_DAYS)
    assert len(keep_days_gauges) == _CAMERA_COUNT
    assert all(g[1] == _LIVE_KEEP_DAYS for g in keep_days_gauges)

    keep_size_gauges = _gauges_named(writer, M_CAMERA_RECORDING_KEEP_SIZE_MB)
    assert len(keep_size_gauges) == _CAMERA_COUNT
    assert all(g[1] == _LIVE_KEEP_SIZE_MB for g in keep_size_gauges)

    retention_gauges = _gauges_named(writer, M_CAMERA_RECORDING_RETENTION_MODE)
    assert len(retention_gauges) == _CAMERA_COUNT
    assert all(g[2].get("mode") == "size" for g in retention_gauges)

    info_cam_num = _gauges_named(writer, M_INFO_CAMERA_NUMBER)
    assert len(info_cam_num) == 1
    assert info_cam_num[0][1] == _LIVE_CAMERA_NUMBER

    info_lic_used = _gauges_named(writer, M_INFO_LICENSE_USED)
    assert len(info_lic_used) == 1
    assert info_lic_used[0][1] == _LIVE_LICENSE_USED

    info_lic_max = _gauges_named(writer, M_INFO_LICENSE_MAX)
    assert len(info_lic_max) == 1
    assert info_lic_max[0][1] == _LIVE_LICENSE_MAX

    version_gauges = _gauges_named(writer, M_INFO_VERSION)
    assert len(version_gauges) == 1
    assert version_gauges[0][2].get("version") == _LIVE_VERSION


@pytest.mark.asyncio
async def test_camera_disconnected() -> None:
    """Row 2: Single camera with status=2 (disconnected)."""
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [_camera(status=2)]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    connected_gauges = _gauges_named(writer, M_CAMERA_CONNECTED)
    assert len(connected_gauges) == 1
    assert connected_gauges[0][1] == 0.0

    status_gauges = _gauges_named(writer, M_CAMERA_STATUS)
    assert len(status_gauges) == 1
    assert status_gauges[0][1] == _STATUS_DISCONNECTED

    total = _gauges_named(writer, M_CAMERAS_TOTAL)
    assert total[0][1] == 1.0
    connected_rollup = _gauges_named(writer, M_CAMERAS_CONNECTED_TOTAL)
    assert connected_rollup[0][1] == 0.0
    disconnected = _gauges_named(writer, M_CAMERAS_DISCONNECTED_TOTAL)
    assert disconnected[0][1] == 1.0


@pytest.mark.asyncio
async def test_camera_status_missing() -> None:
    """Row 3: Camera without status key."""
    writer = MemoryRetainingMetricsWriter()
    cam = cast("dict[str, object]", {"newName": "Test", "stream1": {"fps": 15}})
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    connected_gauges = _gauges_named(writer, M_CAMERA_CONNECTED)
    assert len(connected_gauges) == 1
    assert connected_gauges[0][1] == 0.0

    status_gauges = _gauges_named(writer, M_CAMERA_STATUS)
    assert len(status_gauges) == 0

    total = _gauges_named(writer, M_CAMERAS_TOTAL)
    assert total[0][1] == 1.0
    connected_rollup = _gauges_named(writer, M_CAMERAS_CONNECTED_TOTAL)
    assert connected_rollup[0][1] == 0.0
    disconnected = _gauges_named(writer, M_CAMERAS_DISCONNECTED_TOTAL)
    assert disconnected[0][1] == 1.0


@pytest.mark.asyncio
async def test_cameras_empty_list() -> None:
    """Row 4: Empty cameras list."""
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(cameras=_cam_resp({"cameras": []}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, M_CAMERA_CONNECTED)) == 0
    total = _gauges_named(writer, M_CAMERAS_TOTAL)
    assert total[0][1] == 0.0
    connected = _gauges_named(writer, M_CAMERAS_CONNECTED_TOTAL)
    assert connected[0][1] == 0.0
    disconnected = _gauges_named(writer, M_CAMERAS_DISCONNECTED_TOTAL)
    assert disconnected[0][1] == 0.0


@pytest.mark.asyncio
async def test_cameras_key_missing() -> None:
    """Row 5: Payload without 'cameras' key."""
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(cameras=_cam_resp({}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, M_CAMERA_CONNECTED)) == 0
    total = _gauges_named(writer, M_CAMERAS_TOTAL)
    assert total[0][1] == 0.0


@pytest.mark.asyncio
async def test_cameras_not_a_list() -> None:
    """Row 6: cameras value is not a list."""
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(cameras=_cam_resp({"cameras": "nope"}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, M_CAMERA_CONNECTED)) == 0
    total = _gauges_named(writer, M_CAMERAS_TOTAL)
    assert total[0][1] == 0.0


@pytest.mark.asyncio
async def test_camera_newname_missing_falls_back_to_id() -> None:
    """Row 7: Camera without newName falls back to str(id)."""
    writer = MemoryRetainingMetricsWriter()
    cam = cast("dict[str, object]", {"id": 7, "status": 1, "stream1": {"fps": 15}})
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    connected_gauges = _gauges_named(writer, M_CAMERA_CONNECTED)
    assert len(connected_gauges) == 1
    assert connected_gauges[0][2].get("camera") == "7"


@pytest.mark.asyncio
async def test_camera_newname_and_id_missing_skipped() -> None:
    """Row 8: Camera with no newName and no id is skipped."""
    writer = MemoryRetainingMetricsWriter()
    cam = cast("dict[str, object]", {"status": 1, "stream1": {"fps": 15}})
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, M_CAMERA_CONNECTED)) == 0
    total = _gauges_named(writer, M_CAMERAS_TOTAL)
    assert total[0][1] == 1.0
    connected = _gauges_named(writer, M_CAMERAS_CONNECTED_TOTAL)
    assert connected[0][1] == 1.0


@pytest.mark.asyncio
async def test_camera_stream1_missing() -> None:
    """Row 9: Camera without stream1 key."""
    writer = MemoryRetainingMetricsWriter()
    cam = cast("dict[str, object]", {"id": 1, "newName": "Test", "status": 1})
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, M_CAMERA_FPS)) == 0
    assert len(_gauges_named(writer, M_CAMERA_RESOLUTION_PIXELS)) == 0
    assert len(_gauges_named(writer, M_CAMERA_RESOLUTION)) == 0
    connected = _gauges_named(writer, M_CAMERA_CONNECTED)
    assert len(connected) == 1


@pytest.mark.asyncio
async def test_camera_stream1_not_a_dict() -> None:
    """Row 10: stream1 is not a dict."""
    writer = MemoryRetainingMetricsWriter()
    cam = cast("dict[str, object]", {"id": 1, "newName": "Test", "status": 1, "stream1": "x"})
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, M_CAMERA_FPS)) == 0
    assert len(_gauges_named(writer, M_CAMERA_RESOLUTION_PIXELS)) == 0
    assert len(_gauges_named(writer, M_CAMERA_RESOLUTION)) == 0


@pytest.mark.asyncio
async def test_camera_fps_missing() -> None:
    """Row 11: stream1 without fps."""
    writer = MemoryRetainingMetricsWriter()
    cam = _camera(fps=0)
    cam_copy = cam.copy()
    stream_copy = cast(
        "dict[str, object]",
        cam_copy["stream1"] if isinstance(cam_copy.get("stream1"), dict) else {},
    )
    stream_copy["resolution"] = "1920x1080"
    del stream_copy["fps"]
    cam_copy["stream1"] = stream_copy
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam_copy]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, M_CAMERA_FPS)) == 0
    pix = _gauges_named(writer, M_CAMERA_RESOLUTION_PIXELS)
    assert len(pix) == 1
    res = _gauges_named(writer, M_CAMERA_RESOLUTION)
    assert len(res) == 1


@pytest.mark.asyncio
async def test_camera_resolution_malformed_no_x() -> None:
    """Row 12: resolution string without 'x' separator."""
    writer = MemoryRetainingMetricsWriter()
    cam = _camera()
    cam_copy = cam.copy()
    stream_copy = cast(
        "dict[str, object]",
        cam_copy["stream1"] if isinstance(cam_copy.get("stream1"), dict) else {},
    )
    stream_copy["resolution"] = "1920"
    cam_copy["stream1"] = stream_copy
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam_copy]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    pix = _gauges_named(writer, M_CAMERA_RESOLUTION_PIXELS)
    assert len(pix) == 0
    res = _gauges_named(writer, M_CAMERA_RESOLUTION)
    assert len(res) == 1
    assert res[0][2].get("resolution") == "1920"


@pytest.mark.asyncio
async def test_camera_resolution_malformed_non_int() -> None:
    """Row 13: resolution string with non-integer parts."""
    writer = MemoryRetainingMetricsWriter()
    cam = _camera()
    cam_copy = cam.copy()
    stream_copy = cast(
        "dict[str, object]",
        cam_copy["stream1"] if isinstance(cam_copy.get("stream1"), dict) else {},
    )
    stream_copy["resolution"] = "axb"
    cam_copy["stream1"] = stream_copy
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam_copy]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    pix = _gauges_named(writer, M_CAMERA_RESOLUTION_PIXELS)
    assert len(pix) == 0
    res = _gauges_named(writer, M_CAMERA_RESOLUTION)
    assert len(res) == 1


@pytest.mark.asyncio
async def test_camera_resolution_three_parts() -> None:
    """Row 14: resolution with too many parts."""
    writer = MemoryRetainingMetricsWriter()
    cam = _camera()
    cam_copy = cam.copy()
    stream_copy = cast(
        "dict[str, object]",
        cam_copy["stream1"] if isinstance(cam_copy.get("stream1"), dict) else {},
    )
    stream_copy["resolution"] = "1x2x3"
    cam_copy["stream1"] = stream_copy
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam_copy]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    pix = _gauges_named(writer, M_CAMERA_RESOLUTION_PIXELS)
    assert len(pix) == 0
    res = _gauges_named(writer, M_CAMERA_RESOLUTION)
    assert len(res) == 1


@pytest.mark.asyncio
async def test_camera_resolution_missing_no_carrier() -> None:
    """Row 15: stream1 without resolution."""
    writer = MemoryRetainingMetricsWriter()
    cam = _camera()
    cam_copy = cam.copy()
    stream_copy = cast(
        "dict[str, object]",
        cam_copy["stream1"] if isinstance(cam_copy.get("stream1"), dict) else {},
    )
    stream_copy["fps"] = 15
    del stream_copy["resolution"]
    cam_copy["stream1"] = stream_copy
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam_copy]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    pix = _gauges_named(writer, M_CAMERA_RESOLUTION_PIXELS)
    assert len(pix) == 0
    res = _gauges_named(writer, M_CAMERA_RESOLUTION)
    assert len(res) == 0


@pytest.mark.asyncio
async def test_camera_keep_days_missing() -> None:
    """Row 16: Camera without recordingKeepDays."""
    writer = MemoryRetainingMetricsWriter()
    cam = _camera()
    cam_copy = cam.copy()
    del cam_copy["recordingKeepDays"]
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam_copy]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    keep_days = _gauges_named(writer, M_CAMERA_RECORDING_KEEP_DAYS)
    assert len(keep_days) == 0
    keep_size = _gauges_named(writer, M_CAMERA_RECORDING_KEEP_SIZE_MB)
    assert len(keep_size) == 1


@pytest.mark.asyncio
async def test_camera_keep_size_missing() -> None:
    """Row 17: Camera without recordingKeepSize."""
    writer = MemoryRetainingMetricsWriter()
    cam = _camera()
    cam_copy = cam.copy()
    del cam_copy["recordingKeepSize"]
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam_copy]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    keep_size = _gauges_named(writer, M_CAMERA_RECORDING_KEEP_SIZE_MB)
    assert len(keep_size) == 0
    keep_days = _gauges_named(writer, M_CAMERA_RECORDING_KEEP_DAYS)
    assert len(keep_days) == 1


@pytest.mark.asyncio
async def test_camera_retention_mode_days() -> None:
    """Row 18: enableRecordingKeepDays=True -> mode='days'."""
    writer = MemoryRetainingMetricsWriter()
    cam = _camera()
    cam_copy = cam.copy()
    cam_copy["enableRecordingKeepDays"] = True
    cam_copy["enableRecordingKeepSize"] = False
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam_copy]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    retention = _gauges_named(writer, M_CAMERA_RECORDING_RETENTION_MODE)
    assert len(retention) == 1
    assert retention[0][2].get("mode") == "days"


@pytest.mark.asyncio
async def test_camera_retention_mode_size() -> None:
    """Row 19: enableRecordingKeepSize=True (days False) -> mode='size'."""
    writer = MemoryRetainingMetricsWriter()
    cam = _camera()
    cam_copy = cam.copy()
    cam_copy["enableRecordingKeepDays"] = False
    cam_copy["enableRecordingKeepSize"] = True
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam_copy]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    retention = _gauges_named(writer, M_CAMERA_RECORDING_RETENTION_MODE)
    assert len(retention) == 1
    assert retention[0][2].get("mode") == "size"


@pytest.mark.asyncio
async def test_camera_retention_mode_none() -> None:
    """Row 20: Both enable* False -> mode='none'."""
    writer = MemoryRetainingMetricsWriter()
    cam = _camera()
    cam_copy = cam.copy()
    cam_copy["enableRecordingKeepDays"] = False
    cam_copy["enableRecordingKeepSize"] = False
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam_copy]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    retention = _gauges_named(writer, M_CAMERA_RECORDING_RETENTION_MODE)
    assert len(retention) == 1
    assert retention[0][2].get("mode") == "none"


@pytest.mark.asyncio
async def test_camera_fetch_fails_info_ok() -> None:
    """Row 21: Camera fetch fails, info succeeds."""
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(
        cameras=SynologyError(reason="timeout", message="cam timed out"),
        info=_info_resp(_live_info_payload()),
    )
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == ["cam timed out"]
    assert len(_gauges_named(writer, M_CAMERA_CONNECTED)) == 0
    total = _gauges_named(writer, M_CAMERAS_TOTAL)
    assert total[0][1] == 0.0
    info_num = _gauges_named(writer, M_INFO_CAMERA_NUMBER)
    assert len(info_num) == 1
    assert info_num[0][1] == _LIVE_CAMERA_NUMBER


@pytest.mark.asyncio
async def test_camera_info_fetch_fails_camera_ok() -> None:
    """Row 22: Info fetch fails, camera succeeds."""
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(
        cameras=_cam_resp(_live_camera_payload()),
        info=SynologyError(reason="timeout", message="info timed out"),
    )
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert result.errors == ["info timed out"]
    info_num = _gauges_named(writer, M_INFO_CAMERA_NUMBER)
    assert len(info_num) == 1
    assert info_num[0][1] == 0.0
    version = _gauges_named(writer, M_INFO_VERSION)
    assert len(version) == 0
    total = _gauges_named(writer, M_CAMERAS_TOTAL)
    assert total[0][1] == _LIVE_CAMERA_COUNT


@pytest.mark.asyncio
async def test_camera_both_fetches_fail() -> None:
    """Row 23: Both camera and info fetches fail."""
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(
        cameras=SynologyError(reason="timeout", message="cam timed out"),
        info=SynologyError(reason="timeout", message="info timed out"),
    )
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["cam timed out", "info timed out"]
    assert len(_gauges_named(writer, M_CAMERA_CONNECTED)) == 0
    total = _gauges_named(writer, M_CAMERAS_TOTAL)
    assert total[0][1] == 0.0
    drop_gauges = _gauges_named(writer, _DROP)
    assert len(drop_gauges) == _FAMILY_COUNT


@pytest.mark.asyncio
async def test_camera_unconfigured_client() -> None:
    """Row 24: synology client not configured."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, None))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is False
    assert result.errors == ["synology client not configured"]
    assert result.metrics_emitted == 0
    assert len(writer.gauges) == 0


@pytest.mark.asyncio
async def test_camera_payload_not_a_dict() -> None:
    """Row 25: Camera payload is not a dict."""
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(cameras=_cam_resp("nope"), info=_info_resp(_live_info_payload()))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, M_CAMERA_CONNECTED)) == 0
    total = _gauges_named(writer, M_CAMERAS_TOTAL)
    assert total[0][1] == 0.0
    info_num = _gauges_named(writer, M_INFO_CAMERA_NUMBER)
    assert len(info_num) == 1
    assert info_num[0][1] == _LIVE_CAMERA_NUMBER


@pytest.mark.asyncio
async def test_camera_info_payload_not_a_dict() -> None:
    """Row 26: Info payload is not a dict."""
    writer = MemoryRetainingMetricsWriter()
    fake = _FakeSynology(cameras=_cam_resp(_live_camera_payload()), info=_info_resp(123))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    info_num = _gauges_named(writer, M_INFO_CAMERA_NUMBER)
    assert len(info_num) == 1
    assert info_num[0][1] == 0.0
    version = _gauges_named(writer, M_INFO_VERSION)
    assert len(version) == 0
    total = _gauges_named(writer, M_CAMERAS_TOTAL)
    assert total[0][1] == _LIVE_CAMERA_COUNT


@pytest.mark.asyncio
async def test_camera_info_camera_number_missing() -> None:
    """Row 27: cameraNumber missing from info payload."""
    writer = MemoryRetainingMetricsWriter()
    info_payload = _live_info_payload().copy()
    del info_payload["cameraNumber"]
    fake = _FakeSynology(
        cameras=_cam_resp(_live_camera_payload()),
        info=_info_resp(info_payload),
    )
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    info_num = _gauges_named(writer, M_INFO_CAMERA_NUMBER)
    assert len(info_num) == 1
    assert info_num[0][1] == 0.0


@pytest.mark.asyncio
async def test_camera_info_license_number_non_numeric() -> None:
    """Row 28: liscenseNumber is not numeric."""
    writer = MemoryRetainingMetricsWriter()
    info_payload = _live_info_payload().copy()
    info_payload["liscenseNumber"] = "foo"
    fake = _FakeSynology(
        cameras=_cam_resp(_live_camera_payload()),
        info=_info_resp(info_payload),
    )
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    info_lic = _gauges_named(writer, M_INFO_LICENSE_USED)
    assert len(info_lic) == 1
    assert info_lic[0][1] == 0.0


@pytest.mark.asyncio
async def test_camera_info_license_max_missing() -> None:
    """Row 29: maxCameraSupport missing from info payload."""
    writer = MemoryRetainingMetricsWriter()
    info_payload = _live_info_payload().copy()
    del info_payload["maxCameraSupport"]
    fake = _FakeSynology(
        cameras=_cam_resp(_live_camera_payload()),
        info=_info_resp(info_payload),
    )
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    info_lic_max = _gauges_named(writer, M_INFO_LICENSE_MAX)
    assert len(info_lic_max) == 1
    assert info_lic_max[0][1] == 0.0


@pytest.mark.asyncio
async def test_camera_version_missing() -> None:
    """Row 30: version key missing from info payload."""
    writer = MemoryRetainingMetricsWriter()
    info_payload = _live_info_payload().copy()
    del info_payload["version"]
    fake = _FakeSynology(
        cameras=_cam_resp(_live_camera_payload()),
        info=_info_resp(info_payload),
    )
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    version = _gauges_named(writer, M_INFO_VERSION)
    assert len(version) == 0


@pytest.mark.asyncio
async def test_camera_version_partial_missing_subkey() -> None:
    """Row 31: version missing 'build' subkey."""
    writer = MemoryRetainingMetricsWriter()
    info_payload = _live_info_payload().copy()
    version = cast(
        "dict[str, object]",
        info_payload["version"] if isinstance(info_payload.get("version"), dict) else {},
    )
    del version["build"]
    info_payload["version"] = version
    fake = _FakeSynology(
        cameras=_cam_resp(_live_camera_payload()),
        info=_info_resp(info_payload),
    )
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    version_gauges = _gauges_named(writer, M_INFO_VERSION)
    assert len(version_gauges) == 0


@pytest.mark.asyncio
async def test_camera_version_subkey_non_str() -> None:
    """Row 32: version major is an int, not a string."""
    writer = MemoryRetainingMetricsWriter()
    info_payload = _live_info_payload().copy()
    version = cast(
        "dict[str, object]",
        info_payload["version"] if isinstance(info_payload.get("version"), dict) else {},
    )
    version["major"] = 9
    info_payload["version"] = version
    fake = _FakeSynology(
        cameras=_cam_resp(_live_camera_payload()),
        info=_info_resp(info_payload),
    )
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    version_gauges = _gauges_named(writer, M_INFO_VERSION)
    assert len(version_gauges) == 0


@pytest.mark.asyncio
async def test_camera_version_subkey_empty_str() -> None:
    """Row 33: version major is an empty string."""
    writer = MemoryRetainingMetricsWriter()
    info_payload = _live_info_payload().copy()
    version = cast(
        "dict[str, object]",
        info_payload["version"] if isinstance(info_payload.get("version"), dict) else {},
    )
    version["major"] = ""
    info_payload["version"] = version
    fake = _FakeSynology(
        cameras=_cam_resp(_live_camera_payload()),
        info=_info_resp(info_payload),
    )
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    version_gauges = _gauges_named(writer, M_INFO_VERSION)
    assert len(version_gauges) == 0


@pytest.mark.asyncio
async def test_camera_metrics_emitted_accounting() -> None:
    """Row 34: Verify metrics_emitted accounting (full live shape)."""
    writer = MemoryRetainingMetricsWriter()
    ctx = cast("CollectorContext", _ctx(writer, _FakeSynology()))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    api_took_gauges = _gauges_named(writer, _API_TOOK)
    assert len(api_took_gauges) == _EXPECTED_API_TOOK_COUNT
    drop_gauges = _gauges_named(writer, _DROP)
    assert len(drop_gauges) == _FAMILY_COUNT
    assert result.metrics_emitted == len(writer.gauges)


@pytest.mark.asyncio
async def test_camera_info_label_id_none() -> None:
    """Row 35: Camera with id=None -> _str_label returns ''."""
    writer = MemoryRetainingMetricsWriter()
    cam = cast("dict[str, object]", {"newName": "X", "id": None, "status": 1, "stream1": {}})
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    info_gauges = _gauges_named(writer, M_CAMERA_INFO)
    assert len(info_gauges) == 1
    assert info_gauges[0][2].get("id") == ""


@pytest.mark.asyncio
async def test_camera_info_label_id_bool() -> None:
    """Row 35b: Camera with id=True -> _str_label returns 'True'."""
    writer = MemoryRetainingMetricsWriter()
    cam = cast("dict[str, object]", {"newName": "Y", "id": True, "status": 1, "stream1": {}})
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    info_gauges = _gauges_named(writer, M_CAMERA_INFO)
    assert len(info_gauges) == 1
    assert info_gauges[0][2].get("id") == "True"


@pytest.mark.asyncio
async def test_camera_label_id_is_bool_no_name() -> None:
    """Row 36: Camera with id=bool and no newName -> _camera_label returns None."""
    writer = MemoryRetainingMetricsWriter()
    cam = cast("dict[str, object]", {"id": True, "status": 1, "stream1": {}})
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    assert len(_gauges_named(writer, M_CAMERA_CONNECTED)) == 0
    total = _gauges_named(writer, M_CAMERAS_TOTAL)
    assert total[0][1] == 1.0
    connected = _gauges_named(writer, M_CAMERAS_CONNECTED_TOTAL)
    assert connected[0][1] == 1.0


@pytest.mark.asyncio
async def test_camera_label_string_id() -> None:
    """Row 37: Camera with string id, no newName -> label uses string id."""
    writer = MemoryRetainingMetricsWriter()
    cam = cast("dict[str, object]", {"id": "cam-99", "status": 1, "stream1": {}})
    fake = _FakeSynology(cameras=_cam_resp({"cameras": [cam]}))
    ctx = cast("CollectorContext", _ctx(writer, fake))
    collector = SynologyCameraCollector()
    result = await collector.run(ctx)

    assert result.ok is True
    connected_gauges = _gauges_named(writer, M_CAMERA_CONNECTED)
    assert len(connected_gauges) == 1
    assert connected_gauges[0][2].get("camera") == "cam-99"
