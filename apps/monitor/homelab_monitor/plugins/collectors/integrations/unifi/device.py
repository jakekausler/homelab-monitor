"""unifi_device collector -- combined per-device health, ports, radios, PDU relays, temps.

Fetches ``stat/device`` once per 60s tick and emits:

- ``homelab_unifi_device_*`` gauges (up, state, firmware info, update_available,
  uptime, cpu, mem, load, temperature).
- ``homelab_unifi_port_*`` gauges per port in ``port_table``.
- ``homelab_unifi_radio_*`` gauges per radio in ``radio_table_stats``.
- ``homelab_unifi_outlet_relay_state`` per outlet in ``outlet_table`` (PDU only).

One ``await ctx.unifi.stat_device()`` per tick; the ``{"meta":{"rc":"ok"},"data":[...]}``
wrapper is parsed and each device record is emitted atomically in this run.

OK SEMANTICS: a ``UnifiError`` from ``stat_device()`` is a FAILED run
(``ok=False``, errors=[message]). ``ctx.unifi is None`` is also a failed run.
Absent per-device fields (missing arrays, unparseable strings) are silently
skipped -- the run still returns ``ok=True`` for a partial payload.

Graceful-degrade guarantees:
- Every nested list (port_table / radio_table_stats / outlet_table / temperatures)
  is fetched via ``.get()`` and only iterated if ``isinstance(..., list)``.
- Each entry in those lists is only processed if ``isinstance(entry, dict)``.
- STRING numeric fields (poe_power, poe_current, poe_voltage, cpu, mem, loadavg_*)
  are parsed via ``_as_float()`` which returns None on failure -- skipping the gauge.
- ``bool`` is excluded BEFORE ``int``/``float`` isinstance checks in ``_as_float``.
- ``sys_stats`` may be ``{}`` (empty dict) on EdgeSwitch -- graceful skip.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, cast

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult
from homelab_monitor.kernel.unifi.errors import UnifiError


def _as_float(v: object) -> float | None:
    """Parse int, float, or numeric string to float. Returns None for bool, non-numeric, None.

    bool must be excluded FIRST because ``isinstance(True, int)`` is True in Python.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _as_bool(v: object) -> bool:
    """Return bool value if v is a bool, otherwise False."""
    if isinstance(v, bool):
        return v
    return False


def _kind_for(rec: dict[str, object]) -> str:
    """Derive device kind label from type + model + outlet_table presence.

    Rules (in priority order):
    1. model == "USPPDUP" OR non-empty outlet_table list -> "pdu"
    2. type == "udm" -> "gateway"
    3. type == "uap" -> "ap"
    4. type == "usw" -> "switch"
    5. anything else -> "switch"
    """
    model = rec.get("model")
    # NOTE: _emit_outlet_metrics re-reads outlet_table independently; keep both guards in sync.
    outlet_table = rec.get("outlet_table")
    if model == "USPPDUP":
        return "pdu"
    if isinstance(outlet_table, list) and len(cast("list[object]", outlet_table)) > 0:
        return "pdu"
    device_type = rec.get("type")
    if device_type == "udm":
        return "gateway"
    if device_type == "uap":
        return "ap"
    return "switch"


def _emit_numeric(
    ctx: CollectorContext,
    name: str,
    value_obj: object,
    labels: dict[str, str],
    emitted: list[int],
) -> None:
    """Parse value_obj via _as_float and write_gauge if not None; increment emitted[0]."""
    val = _as_float(value_obj)
    if val is not None:
        ctx.vm.write_gauge(name, val, labels)
        emitted[0] += 1


def _emit_device_level(
    ctx: CollectorContext,
    rec: dict[str, object],
    emitted: list[int],
) -> None:
    """Emit all device-level gauges for one device record."""
    name_val = rec.get("name")
    if not isinstance(name_val, str):
        return
    model_val = rec.get("model")
    model = model_val if isinstance(model_val, str) else "unknown"
    kind = _kind_for(rec)

    device_label = {"device": name_val}
    device_model_kind = {"device": name_val, "model": model, "kind": kind}

    # device_up
    state_val = rec.get("state")
    state_f = _as_float(state_val)
    if state_f is not None:
        up = 1.0 if state_f == 1.0 else 0.0
        ctx.vm.write_gauge("homelab_unifi_device_up", up, device_model_kind)
        emitted[0] += 1
        ctx.vm.write_gauge("homelab_unifi_device_state", state_f, device_model_kind)
        emitted[0] += 1

    # firmware_info (info gauge, value=1.0, version labels)
    version_val = rec.get("version")
    dv_val = rec.get("displayable_version")
    if isinstance(version_val, str):
        dv = dv_val if isinstance(dv_val, str) else ""
        ctx.vm.write_gauge(
            "homelab_unifi_device_firmware_info",
            1.0,
            {"device": name_val, "version": version_val, "displayable_version": dv},
        )
        emitted[0] += 1

    # update_available from upgradable (bool)
    upgradable_val = rec.get("upgradable")
    if isinstance(upgradable_val, bool):
        ctx.vm.write_gauge(
            "homelab_unifi_device_update_available",
            1.0 if upgradable_val else 0.0,
            device_label,
        )
        emitted[0] += 1

    # uptime (numeric, seconds elapsed)
    _emit_numeric(
        ctx, "homelab_unifi_device_uptime_seconds", rec.get("uptime"), device_label, emitted
    )

    # cpu/mem from system-stats (string values)
    sys_stats_raw = rec.get("system-stats")
    if isinstance(sys_stats_raw, dict):
        sys_stats: dict[str, object] = cast("dict[str, object]", sys_stats_raw)
        _emit_numeric(
            ctx, "homelab_unifi_device_cpu_percent", sys_stats.get("cpu"), device_label, emitted
        )
        _emit_numeric(
            ctx, "homelab_unifi_device_mem_percent", sys_stats.get("mem"), device_label, emitted
        )

    # load from sys_stats (may be {} on EdgeSwitch)
    sys_stats2_raw = rec.get("sys_stats")
    if isinstance(sys_stats2_raw, dict):
        sys_stats2: dict[str, object] = cast("dict[str, object]", sys_stats2_raw)
        _emit_numeric(
            ctx,
            "homelab_unifi_device_load1",
            sys_stats2.get("loadavg_1"),
            device_label,
            emitted,
        )

    # temperatures array (UDM only; absent on switch/AP/PDU)
    temperatures = rec.get("temperatures")
    if isinstance(temperatures, list):
        for entry_obj in cast("list[object]", temperatures):
            if not isinstance(entry_obj, dict):
                continue
            entry = cast("dict[str, object]", entry_obj)
            temp_name = entry.get("name")
            temp_type = entry.get("type")
            temp_value = entry.get("value")
            if not isinstance(temp_name, str) or not isinstance(temp_type, str):
                continue
            val = _as_float(temp_value)
            if val is not None:
                ctx.vm.write_gauge(
                    "homelab_unifi_device_temperature_celsius",
                    val,
                    {"device": name_val, "name": temp_name, "type": temp_type},
                )
                emitted[0] += 1


def _emit_port_metrics(
    ctx: CollectorContext,
    rec: dict[str, object],
    device_name: str,
    emitted: list[int],
) -> None:
    """Emit per-port gauges from port_table."""
    port_table = rec.get("port_table")
    if not isinstance(port_table, list):
        return
    for entry_obj in cast("list[object]", port_table):
        if not isinstance(entry_obj, dict):
            continue
        entry = cast("dict[str, object]", entry_obj)
        port_idx_f = _as_float(entry.get("port_idx"))
        if port_idx_f is None:
            continue
        port_str = str(int(port_idx_f))
        labels = {"device": device_name, "port": port_str}

        # port_up
        up_val = entry.get("up")
        ctx.vm.write_gauge("homelab_unifi_port_up", 1.0 if _as_bool(up_val) else 0.0, labels)
        emitted[0] += 1

        # port_speed_bps (speed is Mbps int)
        speed_val = entry.get("speed")
        speed_f = _as_float(speed_val)
        if speed_f is not None:
            ctx.vm.write_gauge("homelab_unifi_port_speed_bps", speed_f * 1_000_000, labels)
            emitted[0] += 1

        # PoE fields (STRING-encoded on real UDM)
        _emit_numeric(
            ctx, "homelab_unifi_port_poe_power_watts", entry.get("poe_power"), labels, emitted
        )
        _emit_numeric(
            ctx, "homelab_unifi_port_poe_current_ma", entry.get("poe_current"), labels, emitted
        )
        _emit_numeric(
            ctx, "homelab_unifi_port_poe_voltage", entry.get("poe_voltage"), labels, emitted
        )

        # poe_good (bool, only if present)
        poe_good_val = entry.get("poe_good")
        if isinstance(poe_good_val, bool):
            ctx.vm.write_gauge("homelab_unifi_port_poe_good", 1.0 if poe_good_val else 0.0, labels)
            emitted[0] += 1

        # byte and error counters
        for field, metric in (
            ("rx_bytes", "homelab_unifi_port_rx_bytes"),
            ("tx_bytes", "homelab_unifi_port_tx_bytes"),
            ("rx_errors", "homelab_unifi_port_rx_errors"),
            ("tx_errors", "homelab_unifi_port_tx_errors"),
            ("rx_dropped", "homelab_unifi_port_rx_dropped"),
            ("tx_dropped", "homelab_unifi_port_tx_dropped"),
            ("mac_table_count", "homelab_unifi_port_mac_table_count"),
            ("link_down_count", "homelab_unifi_port_link_down_count"),
            ("satisfaction", "homelab_unifi_port_satisfaction"),
        ):
            _emit_numeric(ctx, metric, entry.get(field), labels, emitted)


def _emit_radio_metrics(
    ctx: CollectorContext,
    rec: dict[str, object],
    device_name: str,
    emitted: list[int],
) -> None:
    """Emit per-radio gauges from radio_table_stats."""
    radio_table = rec.get("radio_table_stats")
    if not isinstance(radio_table, list):
        return
    for entry_obj in cast("list[object]", radio_table):
        if not isinstance(entry_obj, dict):
            continue
        entry = cast("dict[str, object]", entry_obj)
        radio_name_val = entry.get("name")
        if not isinstance(radio_name_val, str):
            continue
        labels = {"device": device_name, "radio": radio_name_val}
        for field, metric in (
            ("cu_total", "homelab_unifi_radio_cu_total"),
            ("cu_self_rx", "homelab_unifi_radio_cu_self_rx"),
            ("cu_self_tx", "homelab_unifi_radio_cu_self_tx"),
            ("num_sta", "homelab_unifi_radio_num_sta"),
            ("tx_power", "homelab_unifi_radio_tx_power"),
            ("tx_retries_pct", "homelab_unifi_radio_tx_retries_pct"),
            ("satisfaction", "homelab_unifi_radio_satisfaction"),
            ("channel", "homelab_unifi_radio_channel"),
            ("bw", "homelab_unifi_radio_bandwidth_mhz"),
        ):
            _emit_numeric(ctx, metric, entry.get(field), labels, emitted)


def _emit_outlet_metrics(
    ctx: CollectorContext,
    rec: dict[str, object],
    device_name: str,
    emitted: list[int],
) -> None:
    """Emit per-outlet relay_state from outlet_table (PDU only). NO power/current/voltage."""
    outlet_table = rec.get("outlet_table")
    if not isinstance(outlet_table, list):
        return
    for entry_obj in cast("list[object]", outlet_table):
        if not isinstance(entry_obj, dict):
            continue
        entry = cast("dict[str, object]", entry_obj)
        index_val = entry.get("index")
        outlet_name_val = entry.get("name")
        relay_val = entry.get("relay_state")
        if index_val is None:
            continue
        outlet_str = str(index_val)
        outlet_name = outlet_name_val if isinstance(outlet_name_val, str) else ""
        labels = {"device": device_name, "outlet": outlet_str, "name": outlet_name}
        ctx.vm.write_gauge(
            "homelab_unifi_outlet_relay_state",
            1.0 if _as_bool(relay_val) else 0.0,
            labels,
        )
        emitted[0] += 1


class UnifiDeviceCollector(BaseCollector):
    """Emit device health, port, radio, outlet, and temperature metrics from stat/device."""

    name: ClassVar[str] = "unifi_device"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "unifi"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Fetch stat/device once and emit all device sub-metric families."""
        start = time.monotonic()

        if ctx.unifi is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["unifi client not configured"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        result = await ctx.unifi.stat_device()
        if isinstance(result, UnifiError):
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=[result.message],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        # Emit API latency gauge on successful response.
        ctx.vm.write_gauge(
            "homelab_unifi_api_took_seconds",
            result.took_seconds,
            {"endpoint": result.endpoint},
        )
        emitted = [1]  # counts write_gauge calls; starts at 1 for the latency gauge above

        payload_obj = result.payload
        if not isinstance(payload_obj, dict):
            return CollectorResult(
                ok=True,
                metrics_emitted=emitted[0],
                errors=[],
                events=[],
                duration_seconds=time.monotonic() - start,
            )
        payload = cast("dict[str, object]", payload_obj)

        data_obj = payload.get("data")
        if not isinstance(data_obj, list):
            return CollectorResult(
                ok=True,
                metrics_emitted=emitted[0],
                errors=[],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        for rec_obj in cast("list[object]", data_obj):
            if not isinstance(rec_obj, dict):
                continue
            rec = cast("dict[str, object]", rec_obj)
            name_val = rec.get("name")
            if not isinstance(name_val, str):
                continue
            _emit_device_level(ctx, rec, emitted)
            _emit_port_metrics(ctx, rec, name_val, emitted)
            _emit_radio_metrics(ctx, rec, name_val, emitted)
            _emit_outlet_metrics(ctx, rec, name_val, emitted)

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted[0],
            errors=[],
            events=[],
            duration_seconds=time.monotonic() - start,
        )
