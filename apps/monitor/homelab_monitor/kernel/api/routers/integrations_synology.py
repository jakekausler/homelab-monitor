"""GET /api/integrations/synology/* — Synology DSM panel read endpoints.

Mirrors the EPIC-005/006/007 integration routers (integrations_unifi.py,
integrations_pihole.py): VictoriaMetrics INSTANT queries via the shared vm_query
helper for the metric-sourced endpoints, plus ONE live-DSM re-query
(/connections) against the SynologyRestClient. Read-only; cookie session
required.

Failure contract:
  - VM transport/query error -> 502 upstream_unavailable (raised inside
    vm_instant_query). NEVER a 200-with-zeros.
  - Integration "down" is represented IN the payload (dsm_up=false, etc.) when
    VM is reachable but the underlying series indicate the subsystem is
    down/absent.
  - /connections: a live SynologyError or unreachable DSM degrades to
    data_available=false with an empty list (NOT a 502); a missing client
    (app.state.synology_client is None) -> 503.

Empty-vector semantics: a successful query with an empty result returns []
(scalar reads default to None/0/false; list reads default to []).

data_available semantics: for the three stale-absent families (ssh-probe,
mount) the relevant endpoint queries homelab_collector_run_success_total{name=...}
and sets data_available = (sample present and value > 0). This distinguishes
"collector never ran / disabled" from "ran and found nothing".

PRIVACY: /connections returns live DSM connection rows to the authenticated
session ONLY. They are NEVER persisted.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
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
from homelab_monitor.kernel.synology.client import SynologyRestClient
from homelab_monitor.kernel.synology.errors import SynologyError

router = APIRouter(prefix="/integrations/synology", tags=["integrations"])


# --- sample-parsing helpers (mirror integrations_unifi.py) ---
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


def _last_seen_iso(samples: list[VmInstantSample]) -> str | None:
    """ISO-8601 UTC timestamp of the first sample, or None when empty."""
    s = first_sample(samples)
    if s is None:
        return None
    return datetime.fromtimestamp(s.ts, tz=UTC).isoformat()


def _escape_label_value(value: str) -> str:
    """Escape a PromQL label value (backslash, double-quote, newline)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _data_available(samples: list[VmInstantSample]) -> bool:
    """True iff a collector self-metric sample is present with value > 0."""
    val = _scalar(samples)
    return val is not None and val > 0


def _get_synology_client(request: Request) -> SynologyRestClient:
    """Return the live SynologyRestClient from app.state, or 503 if absent."""
    client = getattr(request.app.state, "synology_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="synology client is not initialized",
        )
    return cast(SynologyRestClient, client)


# --- PromQL constants ---
# /summary
_Q_HEALTH_OK = "homelab_synology_health_ok"
_Q_VOLUME_USED_PCT_MAX = "max(homelab_synology_volume_used_percent)"
_Q_UPS_ON_BATTERY = "homelab_synology_ups_on_battery"
_Q_UPS_CHARGE_PCT = "homelab_synology_ups_charge_percent"
_Q_DSM_UPDATE_AVAILABLE = "homelab_synology_dsm_update_available"
_Q_SECURITY_SAFE = "homelab_synology_security_safe"
_Q_NO_BACKUP_CONFIGURED = "homelab_synology_no_backup_configured"

# /hardware — storage
_Q_VOLUME_USED_PCT = "homelab_synology_volume_used_percent"
_Q_VOLUME_STATUS = "homelab_synology_volume_status"
_Q_POOL_STATUS = "homelab_synology_pool_status"
_Q_RAID_STATUS = "homelab_synology_raid_status"
_Q_DISK_STATUS = "homelab_synology_disk_status"
_Q_DISK_SMART_STATUS = "homelab_synology_disk_smart_status"
_Q_DISK_TEMP = "homelab_synology_disk_temp_celsius"
_Q_SMART_ATTR_FAILING = "homelab_synology_smart_attr_failing"
# /hardware — system
_Q_SYS_UPTIME = "homelab_synology_system_uptime_seconds"
_Q_SYS_TEMP = "homelab_synology_sys_temp_celsius"
_Q_NEED_REBOOT = "homelab_synology_need_reboot"
_Q_INFO = "homelab_synology_info"
_Q_FAN_STATUS = "homelab_synology_fan_status"
# /hardware — ups
_Q_UPS_CONNECTED = "homelab_synology_ups_connected"
# /hardware — ssh probe
_Q_SSH_LOAD1 = "homelab_synology_ssh_load1"
_Q_SSH_CPU_TEMP = "homelab_synology_ssh_cpu_temp_celsius"
_Q_MDSTAT_DEGRADED = "homelab_synology_mdstat_array_degraded"
_Q_SSH_PROBE_AVAILABLE = 'homelab_collector_run_success_total{name="synology-probe"}'

# /ops — backup
_Q_BACKUP_CONFIGURED_COUNT = "homelab_synology_backup_configured_count"
_Q_BACKUP_LAST_RESULT_OK = "homelab_synology_backup_last_result_ok"
# /ops — replication
_Q_SNAPSHOT_COUNT = "homelab_synology_snapshot_count"
_Q_REPLICATION_AVAILABLE = "homelab_synology_replication_available"
# /ops — updates
_Q_PACKAGES_WITH_UPDATES = "homelab_synology_packages_with_updates_count"
_Q_PACKAGE_UPDATE_AVAILABLE = "homelab_synology_package_update_available"
# /ops — security
_Q_SECURITY_FINDINGS_TOTAL = "homelab_synology_security_findings_total"
# /ops — mounts
_Q_MOUNT_UP = "homelab_synology_mount_up"
_Q_MOUNT_FREE_BYTES = "homelab_synology_mount_free_bytes"
_Q_MOUNT_AVAILABLE = 'homelab_collector_run_success_total{name="synology_mount_health"}'

# /disks/{disk}/smart-attrs
_Q_SMART_ATTR_RAW = "homelab_synology_smart_attr_raw"
_Q_SMART_ATTR_WORST = "homelab_synology_smart_attr_worst"
_Q_SMART_ATTR_THRESHOLD = "homelab_synology_smart_attr_threshold"


# --- /summary models ---
class SynologySummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dsm_up: bool
    volume_used_percent_max: float | None
    ups_on_battery: bool
    ups_charge_percent: float | None
    update_available: bool
    security_safe: bool
    backup_configured: bool
    last_seen: str | None


# --- /hardware models ---
class VolumeRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    volume: str
    used_percent: float | None
    status: str


class PoolRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pool: str
    status: str
    raid_status: str


class DiskRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    disk: str
    status: float | None
    smart_status: float | None
    temp_celsius: float | None
    smart_attr_failing: bool


class FanRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    state: str
    value: float | None


class SynologySystem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    health_ok: bool
    uptime_seconds: float | None
    sys_temp_celsius: float | None
    need_reboot: bool
    model: str | None
    serial: str | None
    firmware: str | None
    fans: list[FanRow]


class SynologyUps(BaseModel):
    model_config = ConfigDict(extra="ignore")

    connected: bool
    on_battery: bool
    charge_percent: float | None


class SynologySshProbe(BaseModel):
    model_config = ConfigDict(extra="ignore")

    load1: float | None
    cpu_temp_celsius: float | None
    mdstat_array_degraded: bool


class SynologyHardware(BaseModel):
    model_config = ConfigDict(extra="ignore")

    volumes: list[VolumeRow]
    pools: list[PoolRow]
    disks: list[DiskRow]
    system: SynologySystem
    ups: SynologyUps
    ssh_probe: SynologySshProbe
    ssh_probe_data_available: bool


# --- /ops models ---
class SynologyBackup(BaseModel):
    model_config = ConfigDict(extra="ignore")

    configured_count: int
    no_backup_configured: bool
    last_result_ok: bool | None


class ReplicationRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    share: str
    snapshot_count: float | None


class SynologyReplication(BaseModel):
    model_config = ConfigDict(extra="ignore")

    shares: list[ReplicationRow]
    replication_available: bool


class PackageUpdateRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    package: str
    update_available: bool


class SynologyUpdates(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dsm_update_available: bool
    packages_with_updates_count: int
    packages: list[PackageUpdateRow]


class SecurityFindingRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    severity: str
    count: float | None


class SynologySecurity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    security_safe: bool
    findings: list[SecurityFindingRow]


class MountRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mount: str
    mount_up: bool
    mount_free_bytes: float | None


class SynologyOps(BaseModel):
    model_config = ConfigDict(extra="ignore")

    backup: SynologyBackup
    replication: SynologyReplication
    updates: SynologyUpdates
    security: SynologySecurity
    mounts: list[MountRow]
    mount_data_available: bool


# --- /disks/{disk}/smart-attrs models ---
class SmartAttrRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    attr_id: str
    attr_name: str
    raw: float | None
    worst: float | None
    threshold: float | None


class SynologyDiskSmartAttrs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    disk: str
    attrs: list[SmartAttrRow]
    data_available: bool


# --- /connections models ---
class ConnectionRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user: str
    ip: str
    type: str


class SynologyConnections(BaseModel):
    model_config = ConfigDict(extra="ignore")

    connections: list[ConnectionRow]
    data_available: bool


@router.get("/summary", response_model=SynologySummary)
async def get_synology_summary(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> SynologySummary:
    """Return Synology DSM panel summary from VictoriaMetrics instant queries.

    Auth: cookie session required. Each scalar defaults to None/False when its
    instant query returns an empty vector. VM transport failure -> 502.
    """
    (
        health_samples,
        vol_pct_samples,
        ups_batt_samples,
        ups_charge_samples,
        update_samples,
        security_samples,
        no_backup_samples,
    ) = await asyncio.gather(
        vm_instant_query(http_client, vm_url, _Q_HEALTH_OK),
        vm_instant_query(http_client, vm_url, _Q_VOLUME_USED_PCT_MAX),
        vm_instant_query(http_client, vm_url, _Q_UPS_ON_BATTERY),
        vm_instant_query(http_client, vm_url, _Q_UPS_CHARGE_PCT),
        vm_instant_query(http_client, vm_url, _Q_DSM_UPDATE_AVAILABLE),
        vm_instant_query(http_client, vm_url, _Q_SECURITY_SAFE),
        vm_instant_query(http_client, vm_url, _Q_NO_BACKUP_CONFIGURED),
    )

    return SynologySummary(
        dsm_up=_bool_metric(health_samples),
        volume_used_percent_max=_scalar(vol_pct_samples),
        ups_on_battery=_bool_metric(ups_batt_samples),
        ups_charge_percent=_scalar(ups_charge_samples),
        update_available=_bool_metric(update_samples),
        security_safe=_bool_metric(security_samples),
        backup_configured=not _bool_metric(no_backup_samples),
        last_seen=_last_seen_iso(health_samples),
    )


@router.get("/hardware", response_model=SynologyHardware)
async def get_synology_hardware(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> SynologyHardware:
    """Return Synology hardware detail (volumes/pools/disks/system/ups/ssh-probe)."""
    (
        vol_pct_samples,
        vol_status_samples,
        pool_status_samples,
        raid_status_samples,
        disk_status_samples,
        disk_smart_samples,
        disk_temp_samples,
        smart_failing_samples,
        uptime_samples,
        sys_temp_samples,
        need_reboot_samples,
        info_samples,
        fan_samples,
        health_samples,
        ups_connected_samples,
        ups_batt_samples,
        ups_charge_samples,
        ssh_load1_samples,
        ssh_cpu_temp_samples,
        mdstat_samples,
        ssh_available_samples,
    ) = await asyncio.gather(
        vm_instant_query(http_client, vm_url, _Q_VOLUME_USED_PCT),
        vm_instant_query(http_client, vm_url, _Q_VOLUME_STATUS),
        vm_instant_query(http_client, vm_url, _Q_POOL_STATUS),
        vm_instant_query(http_client, vm_url, _Q_RAID_STATUS),
        vm_instant_query(http_client, vm_url, _Q_DISK_STATUS),
        vm_instant_query(http_client, vm_url, _Q_DISK_SMART_STATUS),
        vm_instant_query(http_client, vm_url, _Q_DISK_TEMP),
        vm_instant_query(http_client, vm_url, _Q_SMART_ATTR_FAILING),
        vm_instant_query(http_client, vm_url, _Q_SYS_UPTIME),
        vm_instant_query(http_client, vm_url, _Q_SYS_TEMP),
        vm_instant_query(http_client, vm_url, _Q_NEED_REBOOT),
        vm_instant_query(http_client, vm_url, _Q_INFO),
        vm_instant_query(http_client, vm_url, _Q_FAN_STATUS),
        vm_instant_query(http_client, vm_url, _Q_HEALTH_OK),
        vm_instant_query(http_client, vm_url, _Q_UPS_CONNECTED),
        vm_instant_query(http_client, vm_url, _Q_UPS_ON_BATTERY),
        vm_instant_query(http_client, vm_url, _Q_UPS_CHARGE_PCT),
        vm_instant_query(http_client, vm_url, _Q_SSH_LOAD1),
        vm_instant_query(http_client, vm_url, _Q_SSH_CPU_TEMP),
        vm_instant_query(http_client, vm_url, _Q_MDSTAT_DEGRADED),
        vm_instant_query(http_client, vm_url, _Q_SSH_PROBE_AVAILABLE),
    )

    vol_pct_idx = {s.labels.get("volume", ""): _sample_float(s) for s in vol_pct_samples}
    volumes: list[VolumeRow] = [
        VolumeRow(
            volume=s.labels.get("volume", ""),
            used_percent=vol_pct_idx.get(s.labels.get("volume", "")),
            status=s.labels.get("status", ""),
        )
        for s in vol_status_samples
    ]

    raid_idx = {s.labels.get("pool", ""): s.labels.get("raid", "") for s in raid_status_samples}
    pools: list[PoolRow] = [
        PoolRow(
            pool=s.labels.get("pool", ""),
            status=s.labels.get("status", ""),
            raid_status=raid_idx.get(s.labels.get("pool", ""), ""),
        )
        for s in pool_status_samples
    ]

    smart_idx = {s.labels.get("disk", ""): _sample_float(s) for s in disk_smart_samples}
    temp_idx = {s.labels.get("disk", ""): _sample_float(s) for s in disk_temp_samples}
    failing_idx = {
        s.labels.get("disk", ""): (_sample_float(s) == 1.0) for s in smart_failing_samples
    }
    disks: list[DiskRow] = [
        DiskRow(
            disk=s.labels.get("disk", ""),
            status=_sample_float(s),
            smart_status=smart_idx.get(s.labels.get("disk", "")),
            temp_celsius=temp_idx.get(s.labels.get("disk", "")),
            smart_attr_failing=failing_idx.get(s.labels.get("disk", ""), False),
        )
        for s in disk_status_samples
    ]

    fans: list[FanRow] = [
        FanRow(state=s.labels.get("state", ""), value=_sample_float(s)) for s in fan_samples
    ]

    info_sample = first_sample(info_samples)
    model: str | None = None
    serial: str | None = None
    firmware: str | None = None
    if info_sample is not None:
        model = info_sample.labels.get("model")
        serial = info_sample.labels.get("serial")
        firmware = info_sample.labels.get("firmware")

    system = SynologySystem(
        health_ok=_bool_metric(health_samples),
        uptime_seconds=_scalar(uptime_samples),
        sys_temp_celsius=_scalar(sys_temp_samples),
        need_reboot=_bool_metric(need_reboot_samples),
        model=model,
        serial=serial,
        firmware=firmware,
        fans=fans,
    )

    ups = SynologyUps(
        connected=_bool_metric(ups_connected_samples),
        on_battery=_bool_metric(ups_batt_samples),
        charge_percent=_scalar(ups_charge_samples),
    )

    ssh_probe = SynologySshProbe(
        load1=_scalar(ssh_load1_samples),
        cpu_temp_celsius=_scalar(ssh_cpu_temp_samples),
        mdstat_array_degraded=_bool_metric(mdstat_samples),
    )

    return SynologyHardware(
        volumes=volumes,
        pools=pools,
        disks=disks,
        system=system,
        ups=ups,
        ssh_probe=ssh_probe,
        ssh_probe_data_available=_data_available(ssh_available_samples),
    )


@router.get("/ops", response_model=SynologyOps)
async def get_synology_ops(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> SynologyOps:
    """Return Synology ops detail (backup/replication/updates/security/mounts)."""
    (
        backup_count_samples,
        no_backup_samples,
        backup_ok_samples,
        snapshot_samples,
        repl_available_samples,
        dsm_update_samples,
        pkg_count_samples,
        pkg_update_samples,
        findings_samples,
        security_samples,
        mount_up_samples,
        mount_free_samples,
        mount_available_samples,
    ) = await asyncio.gather(
        vm_instant_query(http_client, vm_url, _Q_BACKUP_CONFIGURED_COUNT),
        vm_instant_query(http_client, vm_url, _Q_NO_BACKUP_CONFIGURED),
        vm_instant_query(http_client, vm_url, _Q_BACKUP_LAST_RESULT_OK),
        vm_instant_query(http_client, vm_url, _Q_SNAPSHOT_COUNT),
        vm_instant_query(http_client, vm_url, _Q_REPLICATION_AVAILABLE),
        vm_instant_query(http_client, vm_url, _Q_DSM_UPDATE_AVAILABLE),
        vm_instant_query(http_client, vm_url, _Q_PACKAGES_WITH_UPDATES),
        vm_instant_query(http_client, vm_url, _Q_PACKAGE_UPDATE_AVAILABLE),
        vm_instant_query(http_client, vm_url, _Q_SECURITY_FINDINGS_TOTAL),
        vm_instant_query(http_client, vm_url, _Q_SECURITY_SAFE),
        vm_instant_query(http_client, vm_url, _Q_MOUNT_UP),
        vm_instant_query(http_client, vm_url, _Q_MOUNT_FREE_BYTES),
        vm_instant_query(http_client, vm_url, _Q_MOUNT_AVAILABLE),
    )

    backup_ok_sample = first_sample(backup_ok_samples)
    last_result_ok: bool | None = None
    if backup_ok_sample is not None:
        last_result_ok = _sample_float(backup_ok_sample) == 1.0

    backup = SynologyBackup(
        configured_count=int(_scalar(backup_count_samples) or 0),
        no_backup_configured=_bool_metric(no_backup_samples),
        last_result_ok=last_result_ok,
    )

    shares: list[ReplicationRow] = [
        ReplicationRow(share=s.labels.get("share", ""), snapshot_count=_sample_float(s))
        for s in snapshot_samples
    ]
    replication = SynologyReplication(
        shares=shares,
        replication_available=_bool_metric(repl_available_samples),
    )

    packages: list[PackageUpdateRow] = [
        PackageUpdateRow(
            package=s.labels.get("package", ""),
            update_available=_sample_float(s) == 1.0,
        )
        for s in pkg_update_samples
    ]
    updates = SynologyUpdates(
        dsm_update_available=_bool_metric(dsm_update_samples),
        packages_with_updates_count=int(_scalar(pkg_count_samples) or 0),
        packages=packages,
    )

    findings: list[SecurityFindingRow] = [
        SecurityFindingRow(severity=s.labels.get("severity", ""), count=_sample_float(s))
        for s in findings_samples
    ]
    security = SynologySecurity(
        security_safe=_bool_metric(security_samples),
        findings=findings,
    )

    free_idx = {s.labels.get("mount", ""): _sample_float(s) for s in mount_free_samples}
    mounts: list[MountRow] = [
        MountRow(
            mount=s.labels.get("mount", ""),
            mount_up=_sample_float(s) == 1.0,
            mount_free_bytes=free_idx.get(s.labels.get("mount", "")),
        )
        for s in mount_up_samples
    ]

    return SynologyOps(
        backup=backup,
        replication=replication,
        updates=updates,
        security=security,
        mounts=mounts,
        mount_data_available=_data_available(mount_available_samples),
    )


@router.get("/disks/{disk}/smart-attrs", response_model=SynologyDiskSmartAttrs)
async def get_synology_disk_smart_attrs(
    _user: Annotated[User, Depends(require_session())],
    disk: str,
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> SynologyDiskSmartAttrs:
    """Return per-attribute SMART rows for one disk (from the SSH probe series)."""
    dsk = _escape_label_value(disk)
    (
        raw_samples,
        worst_samples,
        threshold_samples,
        ssh_available_samples,
    ) = await asyncio.gather(
        vm_instant_query(http_client, vm_url, f'{_Q_SMART_ATTR_RAW}{{disk="{dsk}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_SMART_ATTR_WORST}{{disk="{dsk}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_SMART_ATTR_THRESHOLD}{{disk="{dsk}"}}'),
        vm_instant_query(http_client, vm_url, _Q_SSH_PROBE_AVAILABLE),
    )

    worst_idx = {s.labels.get("attr_id", ""): _sample_float(s) for s in worst_samples}
    threshold_idx = {s.labels.get("attr_id", ""): _sample_float(s) for s in threshold_samples}
    attrs: list[SmartAttrRow] = [
        SmartAttrRow(
            attr_id=s.labels.get("attr_id", ""),
            attr_name=s.labels.get("attr_name", ""),
            raw=_sample_float(s),
            worst=worst_idx.get(s.labels.get("attr_id", "")),
            threshold=threshold_idx.get(s.labels.get("attr_id", "")),
        )
        for s in raw_samples
    ]

    return SynologyDiskSmartAttrs(
        disk=disk,
        attrs=attrs,
        data_available=_data_available(ssh_available_samples),
    )


@router.get("/connections", response_model=SynologyConnections)
async def get_synology_connections(
    _user: Annotated[User, Depends(require_session())],
    client: Annotated[SynologyRestClient, Depends(_get_synology_client)],
) -> SynologyConnections:
    """Return the LIVE DSM active connection list (re-queried from the NAS).

    Auth: cookie session required. A live SynologyError / unreachable DSM
    degrades to data_available=false with an empty list (NOT a 502). A missing
    client -> 503 (via the dependency). PRIVACY: NEVER persisted.
    """
    result = await client.current_connection_list()
    if isinstance(result, SynologyError):
        return SynologyConnections(connections=[], data_available=False)

    connections: list[ConnectionRow] = []
    payload = result.payload
    if isinstance(payload, dict):
        items_raw = cast(dict[str, object], payload).get("items")
        if isinstance(items_raw, list):
            for item in cast(list[object], items_raw):
                if not isinstance(item, dict):
                    continue
                item_dict = cast(dict[str, object], item)
                user_raw = item_dict.get("who", item_dict.get("user", ""))
                ip_raw = item_dict.get("from", item_dict.get("ip", ""))
                type_raw = item_dict.get("type", "")
                connections.append(
                    ConnectionRow(
                        user=str(user_raw) if user_raw is not None else "",
                        ip=str(ip_raw) if ip_raw is not None else "",
                        type=str(type_raw) if type_raw is not None else "",
                    )
                )

    return SynologyConnections(connections=connections, data_available=True)
