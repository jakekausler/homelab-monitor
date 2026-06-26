"""synology_cameras collector — Surveillance Station per-camera + SS.Info metrics.

EPIC-008 STAGE-008-015 (Option A). Fetches TWO CO-EQUAL DSM APIs once per 60s tick:
  - SYNO.SurveillanceStation.Camera/List -> cameras[] (per-camera state + recording cfg)
  - SYNO.SurveillanceStation.Info/GetInfo -> SS package counts + version

CO-EQUAL COMBINE (mirrors STAGE-008-013 security.py): there is NO primary. ``_fetch``
records-and-continues on EITHER fetch's client error; the run is ok=False ONLY when BOTH
fetches fail (``ok = cam_resp is not None or info_resp is not None``). A single-fetch
failure is a DEGRADED ok=True run. ``_emit`` ALWAYS runs. An unconfigured client is ok=False.

SEEDING (alertable empty-NAS contract):
  - The 3 top-level rollups (cameras_total / cameras_connected_total /
    cameras_disconnected_total) are SEEDED 0.0 in ``_Built.__init__`` and OVERWRITTEN with
    the real counts when the camera fetch + payload parse. A failed camera fetch leaves the
    seeded 0.0 series, so they ALWAYS emit.
  - The 3 SS.Info scalars (info_camera_number / info_license_used / info_license_max) are
    SEEDED 0.0 and OVERWRITTEN on a successful info parse. A failed info fetch leaves 0.0.
  - The per-camera families are EMIT-ON-PRESENCE (you cannot seed an unknown camera set):
    empty when the camera fetch fails / zero cameras.
  - The version carrier (info_version{version}) is EMIT-ON-PRESENCE: emitted only when all
    four version sub-keys parse to a non-empty assembled string.

PER-CAMERA ISOLATION (mirrors storage.py): each camera record is parsed in isolation — a
malformed camera (missing newName, non-dict stream1, malformed resolution) emits what it can
and NEVER raises or aborts the loop. The 3 rollups are computed from the SAME cameras[] list,
independent of per-camera parse success.

LABEL KEY: the per-camera ``camera`` label is ``newName``; if ``newName`` is absent/empty it
falls back to ``str(id)``; if BOTH are absent the camera is SKIPPED entirely (no label key).

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
    as_list_of_dicts,
    cap_for_synology,
    capped_emitter,
    client_unconfigured_result,
    fetch_or_result,
    nested,
)

# --- Per-camera metric family names (emit-on-presence) --------------------------
M_CAMERA_CONNECTED: Final[str] = "homelab_synology_ss_camera_connected"
M_CAMERA_STATUS: Final[str] = "homelab_synology_ss_camera_status"
M_CAMERA_INFO: Final[str] = "homelab_synology_ss_camera_info"
M_CAMERA_FPS: Final[str] = "homelab_synology_ss_camera_fps"
M_CAMERA_RESOLUTION_PIXELS: Final[str] = "homelab_synology_ss_camera_resolution_pixels"
M_CAMERA_RESOLUTION: Final[str] = "homelab_synology_ss_camera_resolution"
M_CAMERA_RECORDING_KEEP_DAYS: Final[str] = "homelab_synology_ss_camera_recording_keep_days"
M_CAMERA_RECORDING_KEEP_SIZE_MB: Final[str] = "homelab_synology_ss_camera_recording_keep_size_mb"
M_CAMERA_RECORDING_RETENTION_MODE: Final[str] = (
    "homelab_synology_ss_camera_recording_retention_mode"
)

# --- Top-level rollups from cameras[] (SEEDED 0.0 — always emit) ----------------
M_CAMERAS_TOTAL: Final[str] = "homelab_synology_ss_cameras_total"
M_CAMERAS_CONNECTED_TOTAL: Final[str] = "homelab_synology_ss_cameras_connected_total"
M_CAMERAS_DISCONNECTED_TOTAL: Final[str] = "homelab_synology_ss_cameras_disconnected_total"

# --- SS.Info families (scalars SEEDED 0.0; version carrier emit-on-presence) ----
M_INFO_CAMERA_NUMBER: Final[str] = "homelab_synology_ss_info_camera_number"
M_INFO_LICENSE_USED: Final[str] = "homelab_synology_ss_info_license_used"
M_INFO_LICENSE_MAX: Final[str] = "homelab_synology_ss_info_license_max"
M_INFO_VERSION: Final[str] = "homelab_synology_ss_info_version"

# DSM "connected" camera status sentinel (1=connected; 2=disconnected, 3=disabled).
_STATUS_CONNECTED: Final[float] = 1.0

# Resolution string split into width and height parts.
_RESOLUTION_PARTS: Final[int] = 2

# Retention-mode state-set values.
_MODE_DAYS: Final[str] = "days"
_MODE_SIZE: Final[str] = "size"
_MODE_NONE: Final[str] = "none"


# ---------------------------------------------------------------------------
# Multi-fetch wrapper: record-and-continue for INDEPENDENT fetches
# (copied verbatim from STAGE-008-013 security.py)
# ---------------------------------------------------------------------------


def _fetch(
    ctx: CollectorContext,
    response: SynologyResponse | SynologyError,
    start: float,
    emitted: list[int],
    errors: list[str],
) -> SynologyResponse | None:
    """Wrap fetch_or_result for INDEPENDENT (non-early-returning) fetches.

    On a client error fetch_or_result returns a CollectorResult (errors populated); we
    record those error strings into ``errors`` and return None instead of aborting. On
    success it has already emitted api_took + bumped emitted[0]; we return the response.
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

    The 3 cameras_* rollups and the 3 ss_info_* scalars are SEEDED 0.0 in __init__ so a
    failed/absent fetch still emits the alertable series; a successful parse OVERWRITES the
    relevant list. The per-camera families and the version carrier start empty (emit-on-
    presence).
    """

    __slots__ = (
        "camera_connected_obs",
        "camera_fps_obs",
        "camera_info_obs",
        "camera_recording_keep_days_obs",
        "camera_recording_keep_size_mb_obs",
        "camera_recording_retention_mode_obs",
        "camera_resolution_obs",
        "camera_resolution_pixels_obs",
        "camera_status_obs",
        "cameras_connected_total_obs",
        "cameras_disconnected_total_obs",
        "cameras_total_obs",
        "info_camera_number_obs",
        "info_license_max_obs",
        "info_license_used_obs",
        "info_version_obs",
    )

    def __init__(self) -> None:
        """Initialise lists; seed the 3 rollups + 3 info scalars with 0.0 baselines."""
        # Per-camera (emit-on-presence).
        self.camera_connected_obs: list[tuple[dict[str, str], float]] = []
        self.camera_status_obs: list[tuple[dict[str, str], float]] = []
        self.camera_info_obs: list[tuple[dict[str, str], float]] = []
        self.camera_fps_obs: list[tuple[dict[str, str], float]] = []
        self.camera_resolution_pixels_obs: list[tuple[dict[str, str], float]] = []
        self.camera_resolution_obs: list[tuple[dict[str, str], float]] = []
        self.camera_recording_keep_days_obs: list[tuple[dict[str, str], float]] = []
        self.camera_recording_keep_size_mb_obs: list[tuple[dict[str, str], float]] = []
        self.camera_recording_retention_mode_obs: list[tuple[dict[str, str], float]] = []
        # Top-level rollups (SEEDED 0.0 — always emit).
        self.cameras_total_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.cameras_connected_total_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.cameras_disconnected_total_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        # SS.Info scalars (SEEDED 0.0) + version carrier (emit-on-presence).
        self.info_camera_number_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.info_license_used_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.info_license_max_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.info_version_obs: list[tuple[dict[str, str], float]] = []


# ---------------------------------------------------------------------------
# Local parse helpers
# ---------------------------------------------------------------------------


def _parse_resolution(s: object) -> int | None:
    """Parse a DSM ``stream1.resolution`` "WxH" string to a pixel count (W*H).

    Splits on lowercase 'x'; both parts must be ints. Returns None on any failure
    (not a str, not exactly 2 parts, non-int part) — the malformed-resolution branch.
    """
    if not isinstance(s, str):
        return None
    parts = s.split("x")
    if len(parts) != _RESOLUTION_PARTS:
        return None
    try:
        width = int(parts[0])
        height = int(parts[1])
    except ValueError:
        return None
    return width * height


def _retention_mode(cam: dict[str, object]) -> str:
    """Return the recording-retention mode state from the two enable* bools.

    ``enableRecordingKeepDays`` true -> "days"; else ``enableRecordingKeepSize`` true ->
    "size"; else "none". Non-bool / absent values are treated as False.
    """
    if cam.get("enableRecordingKeepDays") is True:
        return _MODE_DAYS
    if cam.get("enableRecordingKeepSize") is True:
        return _MODE_SIZE
    return _MODE_NONE


def _str_label(v: object) -> str:
    """Stringify a label value defensively: str passes through; numbers via str(); else ""."""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return str(v)
    return ""


def _camera_label(cam: dict[str, object]) -> str | None:
    """Return the ``camera`` label value: newName, else str(id), else None (skip).

    newName (non-empty str) wins. Otherwise an int/float/str id falls back to str(id).
    If neither yields a usable key, returns None and the camera is skipped.
    """
    name = cam.get("newName")
    if isinstance(name, str) and name.strip():
        return name.strip()
    raw_id = cam.get("id")
    if isinstance(raw_id, bool):  # bool is an int subclass — never a valid id key
        return None
    if isinstance(raw_id, (int, float)):
        return str(int(raw_id))
    if isinstance(raw_id, str) and raw_id.strip():
        return raw_id.strip()
    return None


# ---------------------------------------------------------------------------
# Parse passes
# ---------------------------------------------------------------------------


def _parse_camera(built: _Built, cam: dict[str, object]) -> None:
    """Append per-camera observations for one camera record (emit-if-present per field).

    PRIMARY KEY: the ``camera`` label (newName, else str(id)). A camera with no usable
    label key is skipped entirely (no observations).

    status==1 -> connected gauge 1.0; any other / missing status -> 0.0 connected. The raw
    status int is also emitted (camera_status). info is a label-carrier (=1.0). fps,
    resolution_pixels, resolution, recording_keep_days, recording_keep_size_mb, and the
    retention-mode state-set are each emit-if-present.
    """
    camera = _camera_label(cam)
    if camera is None:
        return
    clabels = {"camera": camera}

    status = as_float(cam.get("status"))
    connected = 1.0 if status == _STATUS_CONNECTED else 0.0
    built.camera_connected_obs.append((clabels, connected))
    if status is not None:
        built.camera_status_obs.append((clabels, status))

    # Label-carrier info series (=1.0). All label values stringified defensively.
    built.camera_info_obs.append(
        (
            {
                "camera": camera,
                "id": _str_label(cam.get("id")),
                "model": _str_label(cam.get("model")),
                "vendor": _str_label(cam.get("vendor")),
                "ip": _str_label(cam.get("ip")),
                "mac": _str_label(cam.get("mac")),
            },
            1.0,
        )
    )

    fps = as_float(nested(cam, "stream1", "fps"))
    if fps is not None:
        built.camera_fps_obs.append((clabels, fps))

    raw_resolution = nested(cam, "stream1", "resolution")
    pixels = _parse_resolution(raw_resolution)
    if pixels is not None:
        built.camera_resolution_pixels_obs.append((clabels, float(pixels)))
    if isinstance(raw_resolution, str) and raw_resolution.strip():
        built.camera_resolution_obs.append(
            ({"camera": camera, "resolution": raw_resolution.strip()}, 1.0)
        )

    keep_days = as_float(cam.get("recordingKeepDays"))
    if keep_days is not None:
        built.camera_recording_keep_days_obs.append((clabels, keep_days))

    keep_size = as_float(cam.get("recordingKeepSize"))
    if keep_size is not None:
        built.camera_recording_keep_size_mb_obs.append((clabels, keep_size))

    mode = _retention_mode(cam)
    built.camera_recording_retention_mode_obs.append(({"camera": camera, "mode": mode}, 1.0))


def _parse_cameras(built: _Built, payload: dict[str, object]) -> None:
    """Parse cameras[] -> per-camera families + OVERWRITE the 3 seeded rollups.

    Reads ``payload["cameras"]`` (defensive: missing key / non-list -> []). Computes
    total / connected / disconnected from the raw list (NOT from per-camera parse success,
    so a camera skipped for a missing label still counts toward the rollups). Each camera is
    parsed in isolation.
    """
    cameras = as_list_of_dicts(nested(payload, "cameras"))
    total = float(len(cameras))
    connected = 0.0
    for cam in cameras:
        if as_float(cam.get("status")) == _STATUS_CONNECTED:
            connected += 1.0
        _parse_camera(built, cam)
    built.cameras_total_obs = [({}, total)]
    built.cameras_connected_total_obs = [({}, connected)]
    built.cameras_disconnected_total_obs = [({}, total - connected)]


def _parse_info(built: _Built, payload: dict[str, object]) -> None:
    """Parse SS.Info -> OVERWRITE the 3 seeded scalars + emit the version carrier.

    cameraNumber / liscenseNumber (DSM TYPO, verbatim) / maxCameraSupport are read via
    as_float and OVERWRITE the seeded 0.0 baselines only when numeric (a non-numeric field
    leaves the 0.0 baseline). The version carrier is emitted ONLY when all four sub-keys
    (major/minor/small/build) are present strings -> assembled "{major}.{minor}.{small}-{build}".
    """
    cam_num = as_float(payload.get("cameraNumber"))
    if cam_num is not None:
        built.info_camera_number_obs = [({}, cam_num)]

    lic_used = as_float(payload.get("liscenseNumber"))  # DSM typo: liscenseNumber
    if lic_used is not None:
        built.info_license_used_obs = [({}, lic_used)]

    lic_max = as_float(payload.get("maxCameraSupport"))
    if lic_max is not None:
        built.info_license_max_obs = [({}, lic_max)]

    version = _assemble_version(payload)
    if version is not None:
        built.info_version_obs.append(({"version": version}, 1.0))


def _assemble_version(payload: dict[str, object]) -> str | None:
    """Assemble "{major}.{minor}.{small}-{build}" from the version object, or None.

    All four sub-keys must be non-empty strings (DSM ships them as STRINGS). Any missing /
    non-str / empty sub-key -> None (skip the version carrier).
    """
    parts: list[str] = []
    for key in ("major", "minor", "small", "build"):
        v = nested(payload, "version", key)
        if not isinstance(v, str) or not v.strip():
            return None
        parts.append(v.strip())
    return f"{parts[0]}.{parts[1]}.{parts[2]}-{parts[3]}"


def _emit(
    ctx: CollectorContext, built: _Built, events: list[CollectorEvent], emitted: list[int]
) -> None:
    """Cap-route every family through one CappedEmitter."""
    emitter = capped_emitter(ctx, events)

    def family(name: str, obs: list[tuple[dict[str, str], float]]) -> None:
        emitted[0] += emitter.emit_family(name, cap_for_synology(name), obs) + 1

    # Per-camera (emit-on-presence)
    family(M_CAMERA_CONNECTED, built.camera_connected_obs)
    family(M_CAMERA_STATUS, built.camera_status_obs)
    family(M_CAMERA_INFO, built.camera_info_obs)
    family(M_CAMERA_FPS, built.camera_fps_obs)
    family(M_CAMERA_RESOLUTION_PIXELS, built.camera_resolution_pixels_obs)
    family(M_CAMERA_RESOLUTION, built.camera_resolution_obs)
    family(M_CAMERA_RECORDING_KEEP_DAYS, built.camera_recording_keep_days_obs)
    family(M_CAMERA_RECORDING_KEEP_SIZE_MB, built.camera_recording_keep_size_mb_obs)
    family(M_CAMERA_RECORDING_RETENTION_MODE, built.camera_recording_retention_mode_obs)
    # Top-level rollups (seeded)
    family(M_CAMERAS_TOTAL, built.cameras_total_obs)
    family(M_CAMERAS_CONNECTED_TOTAL, built.cameras_connected_total_obs)
    family(M_CAMERAS_DISCONNECTED_TOTAL, built.cameras_disconnected_total_obs)
    # SS.Info (seeded scalars + version carrier)
    family(M_INFO_CAMERA_NUMBER, built.info_camera_number_obs)
    family(M_INFO_LICENSE_USED, built.info_license_used_obs)
    family(M_INFO_LICENSE_MAX, built.info_license_max_obs)
    family(M_INFO_VERSION, built.info_version_obs)


class SynologyCameraCollector(BaseCollector):
    """Emit Surveillance Station per-camera state + SS.Info from 2 CO-EQUAL DSM APIs.

    Polls once per 60s tick in the ``synology`` concurrency group. Neither fetch is primary:
    a single fetch failing records its error but keeps ok=True; ok=False ONLY when BOTH
    fetches fail. An unconfigured client is ok=False. The cameras_* rollups (seeded 0.0) and
    ss_info_* scalars (seeded 0.0) ALWAYS emit; per-camera families + the version carrier are
    emit-on-presence.
    """

    name: ClassVar[str] = "synology_cameras"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "synology"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Fetch camera list + SS.Info co-equally, parse, emit cap-routed families."""
        start = time.monotonic()
        if ctx.synology is None:
            return client_unconfigured_result(start)

        emitted: list[int] = [0]
        errors: list[str] = []
        events: list[CollectorEvent] = []
        built = _Built()

        # CO-EQUAL fetch 1: camera list (per-camera + rollups).
        cam_resp = _fetch(ctx, await ctx.synology.ss_camera_list(), start, emitted, errors)
        if cam_resp is not None:
            cam_payload = as_dict(cam_resp.payload)
            if cam_payload is not None:
                _parse_cameras(built, cam_payload)

        # CO-EQUAL fetch 2: SS.Info (scalars + version).
        info_resp = _fetch(ctx, await ctx.synology.ss_info(), start, emitted, errors)
        if info_resp is not None:
            info_payload = as_dict(info_resp.payload)
            if info_payload is not None:
                _parse_info(built, info_payload)

        # ALWAYS emit (seeded rollups/scalars emit even on a both-failed run).
        _emit(ctx, built, events, emitted)

        # CO-EQUAL: ok=False ONLY when BOTH fetches failed.
        ok = cam_resp is not None or info_resp is not None
        return CollectorResult(
            ok=ok,
            metrics_emitted=emitted[0],
            errors=errors,
            events=events,
            duration_seconds=time.monotonic() - start,
        )
