"""unifi_wan collector -- WAN/ISP health, last speedtest, and multi-WAN failover state.

Fetches ``stat/health`` once per 30s tick (WAN is the highest-priority signal) and
emits:

- ``homelab_unifi_wan_*`` gauges from the ``www`` subsystem entry (up, latency,
  drops, live throughput).
- ``homelab_unifi_speedtest_*`` gauges from the ``www`` entry's LAST speedtest
  result (only when a speedtest has actually run -- see stale-handling below).
- ``homelab_unifi_wan_failover_*`` gauges from the ``wan`` subsystem entry's
  ``uptime_stats`` (capable / active).

One ``await ctx.unifi.stat_health()`` per tick. The classic
``{"meta":{"rc":"ok"},"data":[...]}`` wrapper is parsed; ``data`` is a LIST of
subsystem entries, each tagged with a ``subsystem`` string. The WAN/ISP data is
split across the ``www`` entry (health/speedtest) and the ``wan`` entry (failover).

OK SEMANTICS: a ``UnifiError`` from ``stat_health()`` is a FAILED run
(``ok=False``, errors=[message]). ``ctx.unifi is None`` is also a failed run.
Absent ``www``/``wan`` entries or unparseable fields are silently skipped -- the
run still returns ``ok=True`` for a partial payload.

STALE-SPEEDTEST HANDLING: ``speedtest_lastrun`` is ALWAYS emitted (even 0 means
"never run"). The download/upload/ping speedtest gauges are emitted ONLY when
``speedtest_lastrun`` parses to a value > 0 (otherwise the UDM's xput_down/up/ping
are all 0 and would be misleading real values).

Read-only: this collector reads the LAST speedtest result; it NEVER triggers one.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, cast

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult
from homelab_monitor.kernel.unifi.errors import UnifiError
from homelab_monitor.plugins.collectors.integrations.unifi._parsing import (
    as_float,
    emit_numeric,
)


def _find_subsystem(data: list[object], name: str) -> dict[str, object] | None:
    """Return the first ``data`` entry whose ``subsystem`` field equals ``name``.

    Defensive: skips non-dict entries and entries with no/other ``subsystem``.
    Returns None if no matching entry is present.
    """
    for item in data:
        if not isinstance(item, dict):
            continue
        entry = cast("dict[str, object]", item)
        if entry.get("subsystem") == name:
            return entry
    return None


def _emit_www_metrics(
    ctx: CollectorContext,
    www: dict[str, object],
    emitted: list[int],
) -> None:
    """Emit WAN health + speedtest gauges from the ``www`` subsystem entry."""
    # wan_up: status is a STRING enum ("ok"/"warning"/"error"); emit only if a str.
    status = www.get("status")
    if isinstance(status, str):
        ctx.vm.write_gauge(
            "homelab_unifi_wan_up",
            1.0 if status == "ok" else 0.0,
            {},
        )
        emitted[0] += 1

    # wan_latency_seconds: latency is INT MILLISECONDS -> convert to seconds.
    latency = as_float(www.get("latency"))
    if latency is not None:
        ctx.vm.write_gauge(
            "homelab_unifi_wan_latency_seconds",
            latency / 1000.0,
            {},
        )
        emitted[0] += 1

    # wan_drops: raw packet-drop count (NOT a percentage).
    emit_numeric(ctx, "homelab_unifi_wan_drops", www.get("drops"), {}, emitted)

    # Live throughput (bytes/sec). NOTE the hyphenated field names.
    emit_numeric(
        ctx,
        "homelab_unifi_wan_xput_down_bytes_per_sec",
        www.get("rx_bytes-r"),
        {},
        emitted,
    )
    emit_numeric(
        ctx,
        "homelab_unifi_wan_xput_up_bytes_per_sec",
        www.get("tx_bytes-r"),
        {},
        emitted,
    )

    _emit_speedtest_metrics(ctx, www, emitted)


def _emit_speedtest_metrics(
    ctx: CollectorContext,
    www: dict[str, object],
    emitted: list[int],
) -> None:
    """Emit speedtest gauges; download/upload/ping only when a speedtest has run."""
    lastrun = as_float(www.get("speedtest_lastrun"))
    # ALWAYS emit lastrun when present (0 signals "never run").
    if lastrun is not None:
        ctx.vm.write_gauge("homelab_unifi_speedtest_lastrun", lastrun, {})
        emitted[0] += 1

    # Only emit the actual speedtest results when a speedtest has actually run.
    if lastrun is None or lastrun <= 0:
        return

    emit_numeric(
        ctx,
        "homelab_unifi_speedtest_download_mbps",
        www.get("xput_down"),
        {},
        emitted,
    )
    emit_numeric(
        ctx,
        "homelab_unifi_speedtest_upload_mbps",
        www.get("xput_up"),
        {},
        emitted,
    )
    # speedtest_ping is INT MILLISECONDS -> convert to seconds.
    ping = as_float(www.get("speedtest_ping"))
    if ping is not None:
        ctx.vm.write_gauge(
            "homelab_unifi_speedtest_ping_seconds",
            ping / 1000.0,
            {},
        )
        emitted[0] += 1


def _emit_failover_metrics(
    ctx: CollectorContext,
    wan: dict[str, object],
    www: dict[str, object],
    emitted: list[int],
) -> None:
    """Emit failover capable/active + optional WAN uptime from the ``wan`` entry."""
    uptime_stats_raw = wan.get("uptime_stats")
    if not isinstance(uptime_stats_raw, dict):
        return
    uptime_stats = cast("dict[str, object]", uptime_stats_raw)

    # failover_capable: more than one WAN key means a secondary is configured.
    ctx.vm.write_gauge(
        "homelab_unifi_wan_failover_capable",
        1.0 if len(uptime_stats) > 1 else 0.0,
        {},
    )
    emitted[0] += 1

    # failover_active: 1.0 only when the primary WAN is down AND a secondary WAN
    # is currently carrying traffic. The active=1.0 branch is fixture-tested -- the
    # live rig has a single active WAN and never exercises it.
    primary_up = www.get("status") == "ok"
    secondary_carrying = False
    for key, value in uptime_stats.items():
        if key == "WAN":
            continue
        if not isinstance(value, dict):
            continue
        peer = cast("dict[str, object]", value)
        uptime = as_float(peer.get("uptime"))
        availability = as_float(peer.get("availability"))
        if (uptime is not None and uptime > 0) or (availability is not None and availability > 0):
            secondary_carrying = True
            break
    ctx.vm.write_gauge(
        "homelab_unifi_wan_failover_active",
        1.0 if (not primary_up and secondary_carrying) else 0.0,
        {},
    )
    emitted[0] += 1

    # Optional: primary WAN uptime in seconds.
    primary_raw = uptime_stats.get("WAN")
    if isinstance(primary_raw, dict):
        primary = cast("dict[str, object]", primary_raw)
        emit_numeric(
            ctx,
            "homelab_unifi_wan_uptime_seconds",
            primary.get("uptime"),
            {},
            emitted,
        )


class UnifiWanCollector(BaseCollector):
    """Emit WAN/ISP health, last-speedtest, and multi-WAN failover gauges.

    Reads ``stat/health`` once per 30s tick and parses the ``www`` (health +
    speedtest) and ``wan`` (failover) subsystem entries from the ``data`` list.
    """

    name: ClassVar[str] = "unifi_wan"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "unifi"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Fetch stat/health and emit WAN/speedtest/failover gauges."""
        start = time.monotonic()
        if ctx.unifi is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["unifi client not configured"],
                duration_seconds=time.monotonic() - start,
            )

        result = await ctx.unifi.stat_health()
        if isinstance(result, UnifiError):
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=[result.message],
                duration_seconds=time.monotonic() - start,
            )

        # The classic endpoint has no meta.took; use the client-measured latency.
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
                duration_seconds=time.monotonic() - start,
            )
        payload = cast("dict[str, object]", payload_obj)

        data_obj = payload.get("data")
        if not isinstance(data_obj, list):
            return CollectorResult(
                ok=True,
                metrics_emitted=emitted[0],
                duration_seconds=time.monotonic() - start,
            )
        data = cast("list[object]", data_obj)

        www = _find_subsystem(data, "www")
        if www is not None:
            _emit_www_metrics(ctx, www, emitted)

        wan = _find_subsystem(data, "wan")
        if wan is not None:
            www_for_failover = www if www is not None else {}
            _emit_failover_metrics(ctx, wan, www_for_failover, emitted)

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted[0],
            duration_seconds=time.monotonic() - start,
        )
