"""GET /api/integrations/unifi/* — Unifi panel / network / client read endpoints.

Mirrors the EPIC-005 Home Assistant router (integrations_home_assistant.py):
VictoriaMetrics INSTANT queries via the shared vm_query helper + SQLite registry
reads via UnifiClientRepo. Read-only; cookie session required.

Failure contract (mirrors HA):
  - VM transport/query error -> 502 upstream_unavailable (via vm_instant_query /
    vm_count). NEVER a 200-with-zeros.
  - Integration "down" is represented IN the payload (e.g. controller_up=false,
    teleport_up=false, wan_up=false) — NOT a 502 — when VM is reachable but the
    underlying metric series indicate the subsystem is down/absent.
  - /clients/{mac} for an unknown mac -> 404 not_found.

Empty-vector semantics: a successful query with an empty result list returns []
(scalar reads default to None/0/false; list reads default to []).
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from homelab_monitor.kernel.api.dependencies import (
    get_http_client,
    get_repo,
    get_vm_url,
    require_session,
)
from homelab_monitor.kernel.api.errors import HttpProblem
from homelab_monitor.kernel.api.vm_query import (
    VmInstantSample,
    first_sample,
    vm_instant_query,
)
from homelab_monitor.kernel.auth.models import User
from homelab_monitor.kernel.config import load_unifi_config
from homelab_monitor.kernel.db.repositories.unifi_clients_repository import UnifiClientRepo
from homelab_monitor.kernel.db.repository import SqliteRepository

router = APIRouter(prefix="/integrations/unifi", tags=["integrations"])

# ── Query expressions (metric names sourced from collectors) ────────────────────
# --- /summary + /controller-health ---
_Q_CONTROLLER_UP = "homelab_unifi_up"
_Q_DEVICES_UP = "count(homelab_unifi_device_state == 1)"
_Q_DEVICES_TOTAL = "count(homelab_unifi_device_state)"
_Q_THREAT_COUNT = "count(homelab_unifi_ips_threat == 1)"
_Q_TELEPORT_UP = "homelab_unifi_teleport_up"
_Q_WAN_UP = "homelab_unifi_wan_up"
_Q_API_TOOK = "homelab_unifi_api_took_seconds"

# --- /devices roster (per-device gauges) ---
_Q_DEVICE_STATE = "homelab_unifi_device_state"
_Q_DEVICE_CPU = "homelab_unifi_device_cpu_percent"
_Q_DEVICE_MEM = "homelab_unifi_device_mem_percent"
_Q_DEVICE_TEMP = "homelab_unifi_device_temperature_celsius"
_Q_DEVICE_UPTIME = "homelab_unifi_device_uptime_seconds"
_Q_DEVICE_UPDATE = "homelab_unifi_device_update_available"

# --- /devices/{device} drill-down (filtered by device label) ---
_Q_PORT_UP = "homelab_unifi_port_up"
_Q_PORT_SPEED = "homelab_unifi_port_speed_bps"
_Q_PORT_POE_POWER = "homelab_unifi_port_poe_power_watts"
_Q_PORT_POE_CURRENT = "homelab_unifi_port_poe_current_ma"
_Q_PORT_POE_VOLTAGE = "homelab_unifi_port_poe_voltage"
_Q_PORT_POE_GOOD = "homelab_unifi_port_poe_good"
_Q_PORT_RX = "homelab_unifi_port_rx_bytes"
_Q_PORT_TX = "homelab_unifi_port_tx_bytes"
_Q_PORT_RX_ERRORS = "homelab_unifi_port_rx_errors"
_Q_PORT_TX_ERRORS = "homelab_unifi_port_tx_errors"
_Q_PORT_RX_DROPPED = "homelab_unifi_port_rx_dropped"
_Q_PORT_TX_DROPPED = "homelab_unifi_port_tx_dropped"
_Q_PORT_MAC_TABLE = "homelab_unifi_port_mac_table_count"
_Q_PORT_LINK_DOWN = "homelab_unifi_port_link_down_count"
_Q_PORT_SAT = "homelab_unifi_port_satisfaction"
_Q_RADIO = "homelab_unifi_radio_cu_total"
_Q_RADIO_CU_SELF_RX = "homelab_unifi_radio_cu_self_rx"
_Q_RADIO_CU_SELF_TX = "homelab_unifi_radio_cu_self_tx"
_Q_RADIO_NUM_STA = "homelab_unifi_radio_num_sta"
_Q_RADIO_TX_POWER = "homelab_unifi_radio_tx_power"
_Q_RADIO_TX_RETRIES = "homelab_unifi_radio_tx_retries_pct"
_Q_RADIO_SAT = "homelab_unifi_radio_satisfaction"
_Q_RADIO_CHANNEL = "homelab_unifi_radio_channel"
_Q_RADIO_BANDWIDTH = "homelab_unifi_radio_bandwidth_mhz"
_Q_OUTLET = "homelab_unifi_outlet_relay_state"

# --- /threats ---
_Q_THREATS = "homelab_unifi_threat"

# --- /dpi (RAW cumulative — rate/clamp is FRONTEND/Grafana, NOT here) ---
_Q_DPI_BYTES = "homelab_unifi_client_dpi_bytes"

# --- /teleport ---
_Q_TELEPORT_VERSION = "homelab_unifi_teleport_version"

# --- /network/wan (CURRENT only; HISTORY via /api/metrics/range proxy) ---
_Q_WAN_LATENCY = "homelab_unifi_wan_latency_seconds"
_Q_WAN_ST_DOWN = "homelab_unifi_speedtest_download_mbps"
_Q_WAN_ST_UP = "homelab_unifi_speedtest_upload_mbps"
_Q_WAN_ST_PING = "homelab_unifi_speedtest_ping_seconds"
_Q_WAN_ST_LASTRUN = "homelab_unifi_speedtest_lastrun"
_Q_WAN_FAILOVER_CAPABLE = "homelab_unifi_wan_failover_capable"
_Q_WAN_FAILOVER_ACTIVE = "homelab_unifi_wan_failover_active"
_Q_WAN_UPTIME = "homelab_unifi_wan_uptime_seconds"
_Q_WAN_XPUT_DOWN = "homelab_unifi_wan_xput_down_bytes_per_sec"
_Q_WAN_XPUT_UP = "homelab_unifi_wan_xput_up_bytes_per_sec"

# --- /network/dhcp ---
_Q_DHCP_POOL_SIZE = "homelab_unifi_dhcp_pool_size"
_Q_DHCP_POOL_START = "homelab_unifi_dhcp_pool_start"
_Q_DHCP_POOL_END = "homelab_unifi_dhcp_pool_end"
_Q_DHCP_RESERVATIONS = "homelab_unifi_dhcp_reservation_count"
_Q_CLIENTS_BY_NETWORK = "homelab_unifi_client_count_by_network"

# --- /network/wifi ---
_Q_WIFI_POOR_SIGNAL = "homelab_unifi_clients_poor_signal"
_Q_WIFI_POOR_SAT = "homelab_unifi_clients_poor_satisfaction"
_Q_WIFI_HIGH_RETRIES = "homelab_unifi_clients_high_retries"
_Q_SSID_CLIENT_COUNT = "homelab_unifi_ssid_client_count"
_Q_CLIENT_BY_BAND = "homelab_unifi_client_count_by_band"
_Q_CLIENT_BY_LINK = "homelab_unifi_client_count_by_link"

# --- /network/dns-posture ---
_Q_DHCP_DNS_PRIMARY = "homelab_unifi_dhcp_dns_primary"

# --- /clients/{mac} per-client VM time-series (filtered by mac label) ---
_Q_CLIENT_SIGNAL = "homelab_unifi_client_signal_dbm"
_Q_CLIENT_TX_RATE = "homelab_unifi_client_tx_rate_bps"
_Q_CLIENT_RX_RATE = "homelab_unifi_client_rx_rate_bps"
_Q_CLIENT_DPI = "homelab_unifi_client_dpi_bytes"


# ── Helper functions ──────────────────────────────────────────────────────────


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


def _get_repo_dep(
    repo: Annotated[SqliteRepository, Depends(get_repo)],
) -> UnifiClientRepo:
    """Construct a UnifiClientRepo from the injected SqliteRepository."""
    return UnifiClientRepo(repo)


def _escape_label_value(value: str) -> str:
    """Escape a PromQL label value (backslash, double-quote, newline) to
    prevent injection."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@dataclasses.dataclass
class _DeviceAccum:
    """Typed accumulator for per-device metric values from VM queries."""

    device: str
    model: str
    kind: str
    state: float | None
    cpu: float | None = None
    mem: float | None = None
    temp: float | None = None
    uptime: float | None = None
    update: float | None = None


# ── Pydantic models ───────────────────────────────────────────────────────────


class DnsEnrichment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    top_domains: list[str] = []
    blocked_count: int | None = None
    last_query_at: str | None = None


class UnifiSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    controller_up: bool
    controller_reason: str | None
    devices_up: int
    devices_total: int
    threat_count: int
    teleport_up: bool
    wan_up: bool
    last_seen: str | None


class UnifiNetworkDhcpRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    network: str
    pool_size: float | None
    pool_start: str | None
    pool_end: str | None
    dhcp_enabled: bool
    occupancy: float | None
    reservation_count: int


class UnifiNetworkDhcpResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    networks: list[UnifiNetworkDhcpRow]


class UnifiClientRowModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mac: str
    ip: str | None
    hostname: str | None
    name: str | None
    network: str | None
    online: bool
    use_fixedip: bool
    ap_mac: str | None
    last_seen: str
    lease_expiry: str | None
    is_host: bool


class UnifiClientsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    clients: list[UnifiClientRowModel]
    total: int
    limit: int
    offset: int


class UnifiClientSeries(BaseModel):
    model_config = ConfigDict(extra="ignore")

    signal_dbm: float | None
    tx_rate_bps: float | None
    rx_rate_bps: float | None


class UnifiClientDpiRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    app: str
    cat: str
    bytes: float


class UnifiClientDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mac: str
    ip: str | None
    hostname: str | None
    name: str | None
    oui: str | None
    network: str | None
    ap_mac: str | None
    sw_mac: str | None
    sw_port: int | None
    use_fixedip: bool
    fixed_ip: str | None
    online: bool
    is_host: bool
    first_seen: str
    last_seen: str
    lease_expiry: str | None
    series: UnifiClientSeries
    dpi: list[UnifiClientDpiRow]
    dns: DnsEnrichment | None = None


class UnifiDeviceRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mac: str
    name: str
    model: str
    kind: str
    up: bool
    cpu_pct: float | None
    mem_pct: float | None
    temp: float | None
    uptime_seconds: float | None
    update_available: bool


class UnifiDevicesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    devices: list[UnifiDeviceRow]


class UnifiPortRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    port_idx: str
    up: bool
    speed_bps: float | None
    poe_power_watts: float | None
    poe_current_ma: float | None
    poe_voltage: float | None
    poe_good: bool | None
    rx_bytes: float | None
    tx_bytes: float | None
    rx_errors: float | None
    tx_errors: float | None
    rx_dropped: float | None
    tx_dropped: float | None
    mac_table_count: float | None
    link_down_count: float | None
    satisfaction: float | None


class UnifiRadioRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    radio: str
    cu_total: float | None
    cu_self_rx: float | None
    cu_self_tx: float | None
    num_sta: float | None
    tx_power: float | None
    tx_retries_pct: float | None
    satisfaction: float | None
    channel: float | None
    bandwidth_mhz: float | None


class UnifiOutletRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    outlet: str
    name: str
    relay_state: bool


class UnifiDeviceDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mac: str
    ports: list[UnifiPortRow]
    radios: list[UnifiRadioRow]
    outlets: list[UnifiOutletRow]
    cpu_pct: float | None
    mem_pct: float | None
    temps: list[dict[str, float | str]]
    load: float | None


class UnifiThreatRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    threat_type: str
    count: int


class UnifiThreatsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    threats: list[UnifiThreatRow]


class UnifiDpiRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    client: str
    app: str
    cat: str
    bytes: float


class UnifiDpiResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    apps: list[UnifiDpiRow]


class UnifiTeleport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    teleport_up: bool
    reason: str | None
    version: str | None


class UnifiApiTook(BaseModel):
    model_config = ConfigDict(extra="ignore")

    endpoint: str
    seconds: float


class UnifiControllerHealth(BaseModel):
    model_config = ConfigDict(extra="ignore")

    controller_up: bool
    up_reasons: list[str]
    api_took_seconds: list[UnifiApiTook]


class UnifiWanCurrent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    wan_up: bool
    latency_seconds: float | None
    download_mbps: float | None
    upload_mbps: float | None
    ping_seconds: float | None
    speedtest_lastrun: float | None
    failover_capable: bool
    failover_active: bool
    wan_uptime_seconds: float | None
    xput_down_mbps: float | None
    xput_up_mbps: float | None


class UnifiSsidCount(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ssid: str
    count: int


class UnifiCountByKey(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    count: int


class UnifiWifiResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    poor_signal: int
    poor_satisfaction: int
    high_retries: int
    ssids: list[UnifiSsidCount]
    by_band: list[UnifiCountByKey]
    by_link: list[UnifiCountByKey]


class UnifiDnsHandout(BaseModel):
    model_config = ConfigDict(extra="ignore")

    network: str
    dns: str
    expected_dns: str | None = None
    drift: bool = False


class UnifiDnsPostureResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    networks: list[UnifiDnsHandout]


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/summary", response_model=UnifiSummary)
async def get_unifi_summary(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> UnifiSummary:
    """Return Unifi panel summary counts from VictoriaMetrics instant queries.

    Auth: cookie session required. CSRF NOT enforced on GET.

    Each count defaults to 0 when its instant query returns an empty vector.
    Any VM transport/query failure surfaces as 502 ``upstream_unavailable``
    (via the shared ``vm_query`` helper) rather than a 200-with-zeros response.
    """
    (
        (
            devices_up,
            devices_total,
            threat_count,
        ),
        controller_up_samples,
        teleport_up_samples,
        wan_up_samples,
    ) = await asyncio.gather(
        asyncio.gather(
            vm_instant_query(http_client, vm_url, _Q_DEVICES_UP),
            vm_instant_query(http_client, vm_url, _Q_DEVICES_TOTAL),
            vm_instant_query(http_client, vm_url, _Q_THREAT_COUNT),
        ),
        vm_instant_query(http_client, vm_url, _Q_CONTROLLER_UP),
        vm_instant_query(http_client, vm_url, _Q_TELEPORT_UP),
        vm_instant_query(http_client, vm_url, _Q_WAN_UP),
    )

    controller_up_sample = first_sample(controller_up_samples)
    controller_up = False
    controller_reason: str | None = None
    last_seen: str | None = None
    if controller_up_sample is not None:
        last_seen = datetime.fromtimestamp(controller_up_sample.ts, tz=UTC).isoformat()
        controller_up = _sample_float(controller_up_sample) == 1.0
        if not controller_up:
            controller_reason = controller_up_sample.labels.get("reason")

    return UnifiSummary(
        controller_up=controller_up,
        controller_reason=controller_reason,
        devices_up=int(_scalar(devices_up) or 0),
        devices_total=int(_scalar(devices_total) or 0),
        threat_count=int(_scalar(threat_count) or 0),
        teleport_up=_bool_metric(teleport_up_samples),
        wan_up=_bool_metric(wan_up_samples),
        last_seen=last_seen,
    )


def _build_device_index(
    state_samples: list[VmInstantSample],
) -> dict[str, _DeviceAccum]:
    """Build index per device from state samples."""
    devices_dict: dict[str, _DeviceAccum] = {}
    for sample in state_samples:
        device_name = sample.labels.get("device", "")
        if device_name not in devices_dict:
            devices_dict[device_name] = _DeviceAccum(
                device=device_name,
                model=sample.labels.get("model", ""),
                kind=sample.labels.get("kind", ""),
                state=_sample_float(sample),
            )
    return devices_dict


def _join_device_metrics(
    devices_dict: dict[str, _DeviceAccum],
    metric_samples: dict[str, list[VmInstantSample]],
) -> None:
    """Join CPU/mem/temp/uptime/update metrics into the device index."""
    metric_keys = ["cpu", "mem", "temp", "uptime", "update"]
    for metric_key in metric_keys:
        samples = metric_samples.get(metric_key, [])
        for sample in samples:
            device_name = sample.labels.get("device", "")
            if device_name in devices_dict:
                setattr(devices_dict[device_name], metric_key, _sample_float(sample))


@router.get("/devices", response_model=UnifiDevicesResponse)
async def get_unifi_devices(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> UnifiDevicesResponse:
    """Return roster of all Unifi devices with per-device metrics.

    Auth: cookie session required. CSRF NOT enforced on GET.

    Empty vector -> devices=[].
    """
    (
        state_samples,
        cpu_samples,
        mem_samples,
        temp_samples,
        uptime_samples,
        update_samples,
    ) = await asyncio.gather(
        vm_instant_query(http_client, vm_url, _Q_DEVICE_STATE),
        vm_instant_query(http_client, vm_url, _Q_DEVICE_CPU),
        vm_instant_query(http_client, vm_url, _Q_DEVICE_MEM),
        vm_instant_query(http_client, vm_url, _Q_DEVICE_TEMP),
        vm_instant_query(http_client, vm_url, _Q_DEVICE_UPTIME),
        vm_instant_query(http_client, vm_url, _Q_DEVICE_UPDATE),
    )

    devices_dict = _build_device_index(state_samples)
    _join_device_metrics(
        devices_dict,
        {
            "cpu": cpu_samples,
            "mem": mem_samples,
            "temp": temp_samples,
            "uptime": uptime_samples,
            "update": update_samples,
        },
    )

    devices: list[UnifiDeviceRow] = []
    for accum in devices_dict.values():
        devices.append(
            UnifiDeviceRow(
                mac=accum.device,
                name=accum.device,
                model=accum.model,
                kind=accum.kind,
                up=accum.state == 1.0 if accum.state is not None else False,
                cpu_pct=accum.cpu,
                mem_pct=accum.mem,
                temp=accum.temp,
                uptime_seconds=accum.uptime,
                update_available=accum.update == 1.0 if accum.update is not None else False,
            )
        )

    return UnifiDevicesResponse(devices=devices)


@router.get("/devices/{device}", response_model=UnifiDeviceDetail)
async def get_unifi_device(
    _user: Annotated[User, Depends(require_session())],
    device: str,
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> UnifiDeviceDetail:
    """Return drill-down detail for a single device
    (ports/radios/outlets/temps).

    Auth: cookie session required. CSRF NOT enforced on GET.

    device not found -> 404 not_found.
    """
    dev = _escape_label_value(device)
    state_samples = await vm_instant_query(
        http_client, vm_url, f'{_Q_DEVICE_STATE}{{device="{dev}"}}'
    )
    if not state_samples:
        raise HttpProblem(
            status_code=404,
            code="not_found",
            message="no unifi device series for device",
        )

    (
        port_up_samples,
        port_speed_samples,
        port_poe_power_samples,
        port_poe_current_samples,
        port_poe_voltage_samples,
        port_poe_good_samples,
        port_rx_samples,
        port_tx_samples,
        port_rx_err_samples,
        port_tx_err_samples,
        port_rx_drop_samples,
        port_tx_drop_samples,
        port_mac_table_samples,
        port_link_down_samples,
        port_sat_samples,
        radio_cu_samples,
        radio_cu_rx_samples,
        radio_cu_tx_samples,
        radio_sta_samples,
        radio_pwr_samples,
        radio_retry_samples,
        radio_sat_samples,
        radio_ch_samples,
        radio_bw_samples,
        outlet_samples,
        temp_samples,
        cpu_samples,
        mem_samples,
        load_samples,
    ) = await asyncio.gather(
        vm_instant_query(http_client, vm_url, f'{_Q_PORT_UP}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_PORT_SPEED}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_PORT_POE_POWER}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_PORT_POE_CURRENT}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_PORT_POE_VOLTAGE}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_PORT_POE_GOOD}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_PORT_RX}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_PORT_TX}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_PORT_RX_ERRORS}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_PORT_TX_ERRORS}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_PORT_RX_DROPPED}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_PORT_TX_DROPPED}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_PORT_MAC_TABLE}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_PORT_LINK_DOWN}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_PORT_SAT}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_RADIO}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_RADIO_CU_SELF_RX}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_RADIO_CU_SELF_TX}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_RADIO_NUM_STA}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_RADIO_TX_POWER}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_RADIO_TX_RETRIES}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_RADIO_SAT}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_RADIO_CHANNEL}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_RADIO_BANDWIDTH}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_OUTLET}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_DEVICE_TEMP}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_DEVICE_CPU}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_DEVICE_MEM}{{device="{dev}"}}'),
        vm_instant_query(http_client, vm_url, f'homelab_unifi_device_load1{{device="{dev}"}}'),
    )

    def _by_port(samples: list[VmInstantSample]) -> dict[str, float | None]:
        return {s.labels.get("port", ""): _sample_float(s) for s in samples}

    port_up_idx = {s.labels.get("port", ""): s for s in port_up_samples}
    speed_idx = _by_port(port_speed_samples)
    poe_pwr_idx = _by_port(port_poe_power_samples)
    poe_cur_idx = _by_port(port_poe_current_samples)
    poe_volt_idx = _by_port(port_poe_voltage_samples)
    poe_good_idx = _by_port(port_poe_good_samples)
    rx_idx = _by_port(port_rx_samples)
    tx_idx = _by_port(port_tx_samples)
    rx_err_idx = _by_port(port_rx_err_samples)
    tx_err_idx = _by_port(port_tx_err_samples)
    rx_drop_idx = _by_port(port_rx_drop_samples)
    tx_drop_idx = _by_port(port_tx_drop_samples)
    mac_tbl_idx = _by_port(port_mac_table_samples)
    lnk_dn_idx = _by_port(port_link_down_samples)
    port_sat_idx = _by_port(port_sat_samples)

    ports: list[UnifiPortRow] = []
    for port_idx_str, up_sample in port_up_idx.items():
        poe_good_val = poe_good_idx.get(port_idx_str)
        ports.append(
            UnifiPortRow(
                port_idx=port_idx_str,
                up=_sample_float(up_sample) == 1.0 if up_sample else False,
                speed_bps=speed_idx.get(port_idx_str),
                poe_power_watts=poe_pwr_idx.get(port_idx_str),
                poe_current_ma=poe_cur_idx.get(port_idx_str),
                poe_voltage=poe_volt_idx.get(port_idx_str),
                poe_good=bool(poe_good_val == 1.0) if poe_good_val is not None else None,
                rx_bytes=rx_idx.get(port_idx_str),
                tx_bytes=tx_idx.get(port_idx_str),
                rx_errors=rx_err_idx.get(port_idx_str),
                tx_errors=tx_err_idx.get(port_idx_str),
                rx_dropped=rx_drop_idx.get(port_idx_str),
                tx_dropped=tx_drop_idx.get(port_idx_str),
                mac_table_count=mac_tbl_idx.get(port_idx_str),
                link_down_count=lnk_dn_idx.get(port_idx_str),
                satisfaction=port_sat_idx.get(port_idx_str),
            )
        )

    def _by_radio(samples: list[VmInstantSample]) -> dict[str, float | None]:
        return {s.labels.get("radio", ""): _sample_float(s) for s in samples}

    radio_cu_idx = {s.labels.get("radio", ""): s for s in radio_cu_samples}
    r_cu_rx_idx = _by_radio(radio_cu_rx_samples)
    r_cu_tx_idx = _by_radio(radio_cu_tx_samples)
    r_sta_idx = _by_radio(radio_sta_samples)
    r_pwr_idx = _by_radio(radio_pwr_samples)
    r_retry_idx = _by_radio(radio_retry_samples)
    r_sat_idx = _by_radio(radio_sat_samples)
    r_ch_idx = _by_radio(radio_ch_samples)
    r_bw_idx = _by_radio(radio_bw_samples)

    radios: list[UnifiRadioRow] = []
    for radio_name, cu_sample in radio_cu_idx.items():
        radios.append(
            UnifiRadioRow(
                radio=radio_name,
                cu_total=_sample_float(cu_sample),
                cu_self_rx=r_cu_rx_idx.get(radio_name),
                cu_self_tx=r_cu_tx_idx.get(radio_name),
                num_sta=r_sta_idx.get(radio_name),
                tx_power=r_pwr_idx.get(radio_name),
                tx_retries_pct=r_retry_idx.get(radio_name),
                satisfaction=r_sat_idx.get(radio_name),
                channel=r_ch_idx.get(radio_name),
                bandwidth_mhz=r_bw_idx.get(radio_name),
            )
        )

    outlets: list[UnifiOutletRow] = []
    for sample in outlet_samples:
        outlet = sample.labels.get("outlet", "")
        name = sample.labels.get("name", "")
        outlets.append(
            UnifiOutletRow(
                outlet=outlet,
                name=name,
                relay_state=_sample_float(sample) == 1.0 if sample else False,
            )
        )

    temps: list[dict[str, float | str]] = []
    for sample in temp_samples:
        temp_name = sample.labels.get("name", "")
        temp_type = sample.labels.get("type", "")
        temps.append(
            {
                "name": temp_name,
                "type": temp_type,
                "celsius": _sample_float(sample) or 0.0,
            }
        )

    cpu = _scalar(cpu_samples)
    mem = _scalar(mem_samples)
    load = _scalar(load_samples)

    return UnifiDeviceDetail(
        mac=device,
        ports=ports,
        radios=radios,
        outlets=outlets,
        cpu_pct=cpu,
        mem_pct=mem,
        temps=temps,
        load=load,
    )


@router.get("/threats", response_model=UnifiThreatsResponse)
async def get_unifi_threats(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> UnifiThreatsResponse:
    """Return threat counts by threat type.

    Auth: cookie session required. CSRF NOT enforced on GET.

    Empty vector -> threats=[].
    """
    threats_samples = await vm_instant_query(http_client, vm_url, _Q_THREATS)

    threats: list[UnifiThreatRow] = []
    for sample in threats_samples:
        threat_type = sample.labels.get("type", "")
        threats.append(
            UnifiThreatRow(
                threat_type=threat_type,
                count=int(_sample_float(sample) or 0),
            )
        )

    return UnifiThreatsResponse(threats=threats)


@router.get("/dpi", response_model=UnifiDpiResponse)
async def get_unifi_dpi(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> UnifiDpiResponse:
    """Return RAW cumulative DPI bytes by client/app/category.

    Auth: cookie session required. CSRF NOT enforced on GET.

    NOTE: Returns RAW cumulative byte counters; rate-per-second + clamp is
    computed by the FRONTEND/Grafana, NOT here.

    Empty vector -> apps=[].
    """
    dpi_samples = await vm_instant_query(http_client, vm_url, _Q_DPI_BYTES)

    apps: list[UnifiDpiRow] = []
    for sample in dpi_samples:
        client = sample.labels.get("client", "")
        app = sample.labels.get("app", "")
        cat = sample.labels.get("cat", "")
        apps.append(
            UnifiDpiRow(
                client=client,
                app=app,
                cat=cat,
                bytes=_sample_float(sample) or 0.0,
            )
        )

    return UnifiDpiResponse(apps=apps)


@router.get("/teleport", response_model=UnifiTeleport)
async def get_unifi_teleport(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> UnifiTeleport:
    """Return Teleport VPN status.

    Auth: cookie session required. CSRF NOT enforced on GET.

    Empty vector -> teleport_up=false, reason/version=None.
    """
    up_samples, version_samples = await asyncio.gather(
        vm_instant_query(http_client, vm_url, _Q_TELEPORT_UP),
        vm_instant_query(http_client, vm_url, _Q_TELEPORT_VERSION),
    )

    teleport_up = _bool_metric(up_samples)
    reason: str | None = None
    if not teleport_up:
        up_sample = first_sample(up_samples)
        if up_sample is not None:
            reason = up_sample.labels.get("reason")

    version: str | None = None
    version_sample = first_sample(version_samples)
    if version_sample is not None:
        version = version_sample.labels.get("version")

    return UnifiTeleport(
        teleport_up=teleport_up,
        reason=reason,
        version=version,
    )


@router.get("/controller-health", response_model=UnifiControllerHealth)
async def get_unifi_controller_health(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> UnifiControllerHealth:
    """Return controller up status + API endpoint latencies.

    Auth: cookie session required. CSRF NOT enforced on GET.

    Empty vector -> controller_up=false, up_reasons=[], api_took_seconds=[].
    """
    controller_samples, took_samples = await asyncio.gather(
        vm_instant_query(http_client, vm_url, _Q_CONTROLLER_UP),
        vm_instant_query(http_client, vm_url, _Q_API_TOOK),
    )

    controller_up = _bool_metric(controller_samples)
    up_reasons: list[str] = []
    for sample in controller_samples:
        if "reason" in sample.labels:
            up_reasons.append(sample.labels["reason"])

    api_took_seconds: list[UnifiApiTook] = []
    for sample in took_samples:
        endpoint = sample.labels.get("endpoint", "")
        api_took_seconds.append(
            UnifiApiTook(
                endpoint=endpoint,
                seconds=_sample_float(sample) or 0.0,
            )
        )

    return UnifiControllerHealth(
        controller_up=controller_up,
        up_reasons=up_reasons,
        api_took_seconds=api_took_seconds,
    )


@router.get("/network/wan", response_model=UnifiWanCurrent)
async def get_unifi_wan(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> UnifiWanCurrent:
    """Return CURRENT WAN metrics (latency, speedtest, failover, uptime).

    Auth: cookie session required. CSRF NOT enforced on GET.

    NOTE: CURRENT WAN only. HISTORY (time-range) is served by the existing
    ``/api/metrics/range`` proxy — this endpoint does NOT return history.

    Empty vector -> each metric scalar defaults to None/False.
    """
    (
        wan_up_samples,
        latency_samples,
        st_down_samples,
        st_up_samples,
        st_ping_samples,
        st_lastrun_samples,
        failover_capable_samples,
        failover_active_samples,
        uptime_samples,
        xput_down_samples,
        xput_up_samples,
    ) = await asyncio.gather(
        vm_instant_query(http_client, vm_url, _Q_WAN_UP),
        vm_instant_query(http_client, vm_url, _Q_WAN_LATENCY),
        vm_instant_query(http_client, vm_url, _Q_WAN_ST_DOWN),
        vm_instant_query(http_client, vm_url, _Q_WAN_ST_UP),
        vm_instant_query(http_client, vm_url, _Q_WAN_ST_PING),
        vm_instant_query(http_client, vm_url, _Q_WAN_ST_LASTRUN),
        vm_instant_query(http_client, vm_url, _Q_WAN_FAILOVER_CAPABLE),
        vm_instant_query(http_client, vm_url, _Q_WAN_FAILOVER_ACTIVE),
        vm_instant_query(http_client, vm_url, _Q_WAN_UPTIME),
        vm_instant_query(http_client, vm_url, _Q_WAN_XPUT_DOWN),
        vm_instant_query(http_client, vm_url, _Q_WAN_XPUT_UP),
    )

    return UnifiWanCurrent(
        wan_up=_bool_metric(wan_up_samples),
        latency_seconds=_scalar(latency_samples),
        download_mbps=_scalar(st_down_samples),
        upload_mbps=_scalar(st_up_samples),
        ping_seconds=_scalar(st_ping_samples),
        speedtest_lastrun=_scalar(st_lastrun_samples),
        failover_capable=_bool_metric(failover_capable_samples),
        failover_active=_bool_metric(failover_active_samples),
        wan_uptime_seconds=_scalar(uptime_samples),
        xput_down_mbps=_scalar(xput_down_samples),
        xput_up_mbps=_scalar(xput_up_samples),
    )


def _index_dhcp_metrics(
    dhcp_samples: dict[str, list[VmInstantSample]],
) -> tuple[
    set[str],
    dict[str, float | None],
    dict[str, str | None],
    dict[str, str | None],
    dict[str, int],
    dict[str, int],
]:
    """Index DHCP metrics by network; return universe and indices."""
    networks_set: set[str] = set()
    pool_size_by_net: dict[str, float | None] = {}
    pool_start_by_net: dict[str, str | None] = {}
    pool_end_by_net: dict[str, str | None] = {}
    reservation_by_net: dict[str, int] = {}
    clients_by_net: dict[str, int] = {}

    for sample in dhcp_samples.get("pool_size", []):
        network = sample.labels.get("network", "")
        if network:
            networks_set.add(network)
            pool_size_by_net[network] = _sample_float(sample)

    for sample in dhcp_samples.get("pool_start", []):
        network = sample.labels.get("network", "")
        if network:
            networks_set.add(network)
            pool_start_by_net[network] = sample.value_str

    for sample in dhcp_samples.get("pool_end", []):
        network = sample.labels.get("network", "")
        if network:
            networks_set.add(network)
            pool_end_by_net[network] = sample.value_str

    for sample in dhcp_samples.get("reservations", []):
        network = sample.labels.get("network", "")
        if network:
            networks_set.add(network)
            reservation_by_net[network] = int(_sample_float(sample) or 0)

    for sample in dhcp_samples.get("clients", []):
        network = sample.labels.get("network", "")
        if network:
            networks_set.add(network)
            clients_by_net[network] = int(_sample_float(sample) or 0)

    return (
        networks_set,
        pool_size_by_net,
        pool_start_by_net,
        pool_end_by_net,
        reservation_by_net,
        clients_by_net,
    )


@router.get("/network/dhcp", response_model=UnifiNetworkDhcpResponse)
async def get_unifi_dhcp(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> UnifiNetworkDhcpResponse:
    """Return DHCP pool sizes, reservations, and occupancy per network.

    Auth: cookie session required. CSRF NOT enforced on GET.

    Network universe = union of network labels across pool-size, reservations,
    and clients-by-network metrics. Occupancy = clients_on_network / pool_size
    (None when pool_size is None or <= 0).

    Empty vector -> networks=[].
    """
    (
        pool_size_samples,
        pool_start_samples,
        pool_end_samples,
        reservation_samples,
        clients_by_network_samples,
    ) = await asyncio.gather(
        vm_instant_query(http_client, vm_url, _Q_DHCP_POOL_SIZE),
        vm_instant_query(http_client, vm_url, _Q_DHCP_POOL_START),
        vm_instant_query(http_client, vm_url, _Q_DHCP_POOL_END),
        vm_instant_query(http_client, vm_url, _Q_DHCP_RESERVATIONS),
        vm_instant_query(http_client, vm_url, _Q_CLIENTS_BY_NETWORK),
    )

    (
        networks_set,
        pool_size_by_net,
        pool_start_by_net,
        pool_end_by_net,
        reservation_by_net,
        clients_by_net,
    ) = _index_dhcp_metrics(
        {
            "pool_size": pool_size_samples,
            "pool_start": pool_start_samples,
            "pool_end": pool_end_samples,
            "reservations": reservation_samples,
            "clients": clients_by_network_samples,
        }
    )

    networks: list[UnifiNetworkDhcpRow] = []
    for network in sorted(networks_set):
        pool_size = pool_size_by_net.get(network)
        occupancy: float | None = None
        if pool_size is not None and pool_size > 0:
            clients_count = clients_by_net.get(network, 0)
            occupancy = clients_count / pool_size

        networks.append(
            UnifiNetworkDhcpRow(
                network=network,
                pool_size=pool_size,
                pool_start=pool_start_by_net.get(network),
                pool_end=pool_end_by_net.get(network),
                dhcp_enabled=(
                    network in pool_size_by_net
                    or network in pool_start_by_net
                    or network in pool_end_by_net
                ),
                occupancy=occupancy,
                reservation_count=reservation_by_net.get(network, 0),
            )
        )

    return UnifiNetworkDhcpResponse(networks=networks)


@router.get("/network/wifi", response_model=UnifiWifiResponse)
async def get_unifi_wifi(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> UnifiWifiResponse:
    """Return WiFi stats (poor signal, poor satisfaction, high retries, SSIDs, by-band, by-link).

    Auth: cookie session required. CSRF NOT enforced on GET.

    Empty vector -> defaults (0 for counts, [] for lists).
    """
    (
        poor_signal_samples,
        poor_sat_samples,
        high_retries_samples,
        ssid_samples,
        by_band_samples,
        by_link_samples,
    ) = await asyncio.gather(
        vm_instant_query(http_client, vm_url, _Q_WIFI_POOR_SIGNAL),
        vm_instant_query(http_client, vm_url, _Q_WIFI_POOR_SAT),
        vm_instant_query(http_client, vm_url, _Q_WIFI_HIGH_RETRIES),
        vm_instant_query(http_client, vm_url, _Q_SSID_CLIENT_COUNT),
        vm_instant_query(http_client, vm_url, _Q_CLIENT_BY_BAND),
        vm_instant_query(http_client, vm_url, _Q_CLIENT_BY_LINK),
    )

    poor_signal = int(_scalar(poor_signal_samples) or 0)
    poor_satisfaction = int(_scalar(poor_sat_samples) or 0)
    high_retries = int(_scalar(high_retries_samples) or 0)

    ssids: list[UnifiSsidCount] = []
    for sample in ssid_samples:
        ssid = sample.labels.get("ssid", "")
        ssids.append(
            UnifiSsidCount(
                ssid=ssid,
                count=int(_sample_float(sample) or 0),
            )
        )

    by_band: list[UnifiCountByKey] = []
    for sample in by_band_samples:
        band = sample.labels.get("band", "")
        by_band.append(
            UnifiCountByKey(
                key=band,
                count=int(_sample_float(sample) or 0),
            )
        )

    by_link: list[UnifiCountByKey] = []
    for sample in by_link_samples:
        link = sample.labels.get("link", "")
        by_link.append(
            UnifiCountByKey(
                key=link,
                count=int(_sample_float(sample) or 0),
            )
        )

    return UnifiWifiResponse(
        poor_signal=poor_signal,
        poor_satisfaction=poor_satisfaction,
        high_retries=high_retries,
        ssids=ssids,
        by_band=by_band,
        by_link=by_link,
    )


@router.get("/network/dns-posture", response_model=UnifiDnsPostureResponse)
async def get_unifi_dns_posture(
    _user: Annotated[User, Depends(require_session())],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> UnifiDnsPostureResponse:
    """Return DNS handout (primary DNS servers) per network.

    Auth: cookie session required. CSRF NOT enforced on GET.

    Each handout carries the expected DNS-steering IP (from
    ``HOMELAB_MONITOR_UNIFI_EXPECTED_DNS_STEERING_IP``) and a ``drift`` flag set
    when the network's handed-out DNS differs from that expected IP. An empty
    expected value (env unset / empty) is treated as "not configured": ``expected_dns``
    is None and ``drift`` is always False (no false positives, no false check-marks).

    Empty vector -> networks=[].
    """
    expected = load_unifi_config().expected_dns_steering_ip or None

    dns_samples = await vm_instant_query(http_client, vm_url, _Q_DHCP_DNS_PRIMARY)

    networks: list[UnifiDnsHandout] = []
    for sample in dns_samples:
        network = sample.labels.get("network", "")
        dns = sample.labels.get("dns", "")
        drift = expected is not None and dns != expected
        networks.append(
            UnifiDnsHandout(
                network=network,
                dns=dns,
                expected_dns=expected,
                drift=drift,
            )
        )

    return UnifiDnsPostureResponse(networks=networks)


@router.get("/clients", response_model=UnifiClientsResponse)
async def get_unifi_clients(
    _user: Annotated[User, Depends(require_session())],
    unifi_repo: Annotated[UnifiClientRepo, Depends(_get_repo_dep)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UnifiClientsResponse:
    """Return paginated list of all known Unifi clients.

    Auth: cookie session required. CSRF NOT enforced on GET.

    Sorted by last_seen DESC (most recent first).

    Query params:
      limit: max clients per page (1-500, default 100)
      offset: pagination offset (default 0)

    Empty registry -> clients=[], total=0.
    """
    rows = await unifi_repo.list_clients()
    total = len(rows)
    page = rows[offset : offset + limit]

    clients: list[UnifiClientRowModel] = [
        UnifiClientRowModel(
            mac=row.mac,
            ip=row.ip,
            hostname=row.hostname,
            name=row.name,
            network=row.network,
            online=row.online,
            use_fixedip=row.use_fixedip,
            ap_mac=row.ap_mac,
            last_seen=row.last_seen,
            lease_expiry=row.lease_expiry,
            is_host=row.is_host,
        )
        for row in page
    ]

    return UnifiClientsResponse(
        clients=clients,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/clients/{mac}", response_model=UnifiClientDetail)
async def get_unifi_client(
    _user: Annotated[User, Depends(require_session())],
    mac: str,
    unifi_repo: Annotated[UnifiClientRepo, Depends(_get_repo_dep)],
    vm_url: Annotated[str, Depends(get_vm_url)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> UnifiClientDetail:
    """Return detailed view of a single Unifi client with series + DPI.

    Auth: cookie session required. CSRF NOT enforced on GET.

    mac not found in registry -> 404 not_found.

    VM transport/query error -> 502 upstream_unavailable (after checking registry).

    dpi: RAW cumulative bytes; rate is frontend-computed.
    dns: always None (EPIC-006 slot).
    """
    row = await unifi_repo.get_client(mac)
    if row is None:
        raise HttpProblem(
            status_code=404,
            code="not_found",
            message="unifi client not found",
        )

    mac_esc = _escape_label_value(mac)
    signal_samples, tx_rate_samples, rx_rate_samples, dpi_samples = await asyncio.gather(
        vm_instant_query(http_client, vm_url, f'{_Q_CLIENT_SIGNAL}{{mac="{mac_esc}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_CLIENT_TX_RATE}{{mac="{mac_esc}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_CLIENT_RX_RATE}{{mac="{mac_esc}"}}'),
        vm_instant_query(http_client, vm_url, f'{_Q_CLIENT_DPI}{{client="{mac_esc}"}}'),
    )

    series = UnifiClientSeries(
        signal_dbm=_scalar(signal_samples),
        tx_rate_bps=_scalar(tx_rate_samples),
        rx_rate_bps=_scalar(rx_rate_samples),
    )

    dpi: list[UnifiClientDpiRow] = []
    for sample in dpi_samples:
        app = sample.labels.get("app", "")
        cat = sample.labels.get("cat", "")
        dpi.append(
            UnifiClientDpiRow(
                app=app,
                cat=cat,
                bytes=_sample_float(sample) or 0.0,
            )
        )

    return UnifiClientDetail(
        mac=row.mac,
        ip=row.ip,
        hostname=row.hostname,
        name=row.name,
        oui=row.oui,
        network=row.network,
        ap_mac=row.ap_mac,
        sw_mac=row.sw_mac,
        sw_port=row.sw_port,
        use_fixedip=row.use_fixedip,
        fixed_ip=row.fixed_ip,
        online=row.online,
        is_host=row.is_host,
        first_seen=row.first_seen,
        last_seen=row.last_seen,
        lease_expiry=row.lease_expiry,
        series=series,
        dpi=dpi,
        dns=None,
    )
