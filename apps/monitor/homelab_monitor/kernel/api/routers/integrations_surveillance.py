"""GET /api/integrations/surveillance/* — Surveillance Station panel read endpoints.

VictoriaMetrics INSTANT queries via the shared vm_query helper. Read-only;
cookie session required. Mirrors integrations_synology.py / integrations_unifi.py.

data_available is sourced from
homelab_collector_run_success_total{name="synology_cameras"} so the UI can
distinguish "Surveillance collector never ran / package absent" from "ran,
zero cameras".
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from homelab_monitor.kernel.api.dependencies import (
    get_http_client,
    get_vm_url,
    require_session,
)
from homelab_monitor.kernel.api.vm_query import (
    VmInstantSample,
    first_sample,
    vm_instant_query,
)
from homelab_monitor.kernel.auth.models import User

router = APIRouter(prefix="/integrations/surveillance", tags=["integrations"])


def _sample_float(sample: VmInstantSample) -> float | None:
    """Parse a VmInstantSample's value_str as float; None on a non-numeric value."""
    try:
        return float(sample.value_str)
    except (ValueError, TypeError):
        return None


def _scalar(samples: list[VmInstantSample]) -> float | None:
    """First sample's float value, or None when empty / non-numeric."""
    s = first_sample(samples)
    return None if s is None else _sample_float(s)


def _bool_metric(samples: list[VmInstantSample]) -> bool:
    """True iff the first sample's value == 1.0 (absent/non-numeric -> False)."""
    return _scalar(samples) == 1.0


def _info_label(info: dict[str, str], key: str) -> str | None:
    """Read an enrichment label, treating an empty string as unknown (None).

    The camera collector always emits model/ip/vendor labels, using "" for a
    missing source value. Coalesce "" -> None so the API reports an honest
    unknown rather than an empty string masquerading as a present value.
    """
    value = info.get(key)
    return value if value else None


def _data_available(samples: list[VmInstantSample]) -> bool:
    """True iff the collector self-metric sample is present with value > 0."""
    val = _scalar(samples)
    return val is not None and val > 0


# --- PromQL constants ---
_Q_LICENSE_USED = "homelab_synology_ss_info_license_used"
_Q_LICENSE_MAX = "homelab_synology_ss_info_license_max"
_Q_HOMEMODE_ON = "homelab_synology_ss_homemode_on"
_Q_CAMERAS_TOTAL = "homelab_synology_ss_cameras_total"
_Q_CAMERAS_CONNECTED = "homelab_synology_ss_cameras_connected_total"
_Q_CAMERAS_DISCONNECTED = "homelab_synology_ss_cameras_disconnected_total"
_Q_CAMERA_CONNECTED = "homelab_synology_ss_camera_connected"
_Q_CAMERA_STATUS = "homelab_synology_ss_camera_status"
_Q_RECORDINGS_COUNT = "homelab_synology_ss_recordings_count"
_Q_RECORDINGS_BYTES = "homelab_synology_ss_recordings_bytes"
_Q_EVENTS_TODAY = "homelab_synology_ss_events_today"
_Q_EVENTS_TOTAL_ALL = "homelab_synology_ss_events_total_all"
_Q_RECORDINGS_TOTAL = "homelab_synology_ss_recordings_total"
_Q_RECORDINGS_BYTES_TOTAL = "homelab_synology_ss_recordings_bytes_total"
_Q_CAMERA_INFO = "homelab_synology_ss_camera_info"
_Q_CAMERAS_AVAILABLE = 'homelab_collector_run_success_total{name="synology_cameras"}'


class SurveillanceSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    license_used: float | None
    license_max: float | None
    homemode_on: bool
    cameras_total: float | None
    cameras_connected_total: float | None
    cameras_disconnected_total: float | None
    data_available: bool


class CameraRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    camera: str
    connected: bool
    status: float | None
    recordings_count: float | None
    recordings_bytes: float | None
    model: str | None
    ip: str | None
    vendor: str | None


class SurveillanceCameras(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cameras: list[CameraRow]
    events_today: float | None
    events_total_all: float | None
    recordings_total: float | None
    recordings_bytes_total: float | None
    data_available: bool


@router.get("/summary", response_model=SurveillanceSummary)
async def get_surveillance_summary(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> SurveillanceSummary:
    """Return Surveillance Station summary (license/homemode/camera rollups)."""
    (
        license_used_samples,
        license_max_samples,
        homemode_samples,
        cameras_total_samples,
        cameras_connected_samples,
        cameras_disconnected_samples,
        available_samples,
    ) = await asyncio.gather(
        vm_instant_query(http_client, vm_url, _Q_LICENSE_USED),
        vm_instant_query(http_client, vm_url, _Q_LICENSE_MAX),
        vm_instant_query(http_client, vm_url, _Q_HOMEMODE_ON),
        vm_instant_query(http_client, vm_url, _Q_CAMERAS_TOTAL),
        vm_instant_query(http_client, vm_url, _Q_CAMERAS_CONNECTED),
        vm_instant_query(http_client, vm_url, _Q_CAMERAS_DISCONNECTED),
        vm_instant_query(http_client, vm_url, _Q_CAMERAS_AVAILABLE),
    )

    return SurveillanceSummary(
        license_used=_scalar(license_used_samples),
        license_max=_scalar(license_max_samples),
        homemode_on=_bool_metric(homemode_samples),
        cameras_total=_scalar(cameras_total_samples),
        cameras_connected_total=_scalar(cameras_connected_samples),
        cameras_disconnected_total=_scalar(cameras_disconnected_samples),
        data_available=_data_available(available_samples),
    )


@router.get("/cameras", response_model=SurveillanceCameras)
async def get_surveillance_cameras(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> SurveillanceCameras:
    """Return per-camera status + activity plus system-wide event scalars."""
    (
        connected_samples,
        status_samples,
        recordings_samples,
        bytes_samples,
        events_today_samples,
        events_total_samples,
        recordings_total_samples,
        recordings_bytes_total_samples,
        info_samples,
        available_samples,
    ) = await asyncio.gather(
        vm_instant_query(http_client, vm_url, _Q_CAMERA_CONNECTED),
        vm_instant_query(http_client, vm_url, _Q_CAMERA_STATUS),
        vm_instant_query(http_client, vm_url, _Q_RECORDINGS_COUNT),
        vm_instant_query(http_client, vm_url, _Q_RECORDINGS_BYTES),
        vm_instant_query(http_client, vm_url, _Q_EVENTS_TODAY),
        vm_instant_query(http_client, vm_url, _Q_EVENTS_TOTAL_ALL),
        vm_instant_query(http_client, vm_url, _Q_RECORDINGS_TOTAL),
        vm_instant_query(http_client, vm_url, _Q_RECORDINGS_BYTES_TOTAL),
        vm_instant_query(http_client, vm_url, _Q_CAMERA_INFO),
        vm_instant_query(http_client, vm_url, _Q_CAMERAS_AVAILABLE),
    )

    status_idx = {s.labels.get("camera", ""): _sample_float(s) for s in status_samples}
    rec_idx = {s.labels.get("camera", ""): _sample_float(s) for s in recordings_samples}
    bytes_idx = {s.labels.get("camera", ""): _sample_float(s) for s in bytes_samples}
    info_idx = {s.labels.get("camera", ""): s.labels for s in info_samples}
    cameras: list[CameraRow] = []
    for s in connected_samples:
        name = s.labels.get("camera", "")
        info = info_idx.get(name, {})
        cameras.append(
            CameraRow(
                camera=name,
                connected=_sample_float(s) == 1.0,
                status=status_idx.get(name),
                recordings_count=rec_idx.get(name),
                recordings_bytes=bytes_idx.get(name),
                model=_info_label(info, "model"),
                ip=_info_label(info, "ip"),
                vendor=_info_label(info, "vendor"),
            )
        )

    return SurveillanceCameras(
        cameras=cameras,
        events_today=_scalar(events_today_samples),
        events_total_all=_scalar(events_total_samples),
        recordings_total=_scalar(recordings_total_samples),
        recordings_bytes_total=_scalar(recordings_bytes_total_samples),
        data_available=_data_available(available_samples),
    )
