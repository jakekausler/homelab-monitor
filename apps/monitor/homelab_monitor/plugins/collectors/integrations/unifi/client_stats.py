"""unifi_client_stats collector -- per-client WiFi stats + experience rollups.

Polls classic ``stat/sta`` once per 60s tick and emits two layers of signal:

1. Six CARDINALITY-CAPPED per-client metric families, each keyed by ``{mac}``
   ONLY (Decision D1). The survivor-mac set is identical across families that
   share the same {mac} candidate set (Decision A2 -- deterministic, no shared
   state): wireless-only families (signal/tx_rate/rx_rate) agree with each
   other and all-client families (uptime/tx_bytes/rx_bytes) agree with each
   other. A wired mac never appears in a wireless-only family. (Across the two
   candidate sets the survivor sets are NOT guaranteed equal -- the capper
   sorts by mac and slices, so a wired mac may displace a wireless one in the
   all-client families.):

   - ``homelab_unifi_client_signal_dbm{mac}``  (wireless only)
   - ``homelab_unifi_client_tx_rate_bps{mac}`` (wireless only; KBPS x1000 -> bps, C1)
   - ``homelab_unifi_client_rx_rate_bps{mac}`` (wireless only; KBPS x1000 -> bps, C1)
   - ``homelab_unifi_client_uptime{mac}``      (all clients)
   - ``homelab_unifi_client_tx_bytes{mac}`` (all; ``tx_bytes`` / wired ``wired-tx_bytes``)
   - ``homelab_unifi_client_rx_bytes{mac}`` (all; ``rx_bytes`` / wired ``wired-rx_bytes``)

   Capped via :class:`CappedEmitter` with ``cap_for("unifi_client_stats")`` (=200).

2. Four BOUNDED experience-rollup gauges (NOT capped -- naturally small), with a
   ``{threshold}`` label carrying the constant that produced the count (B1):

   - ``homelab_unifi_clients_poor_signal{threshold="-70"}`` -- wireless clients signal < -70 dBm
   - ``homelab_unifi_clients_poor_satisfaction{threshold="50"}``   -- all clients satisfaction < 50
   - ``homelab_unifi_clients_high_retries{threshold="10"}``        -- wireless clients retry% > 10
   - ``homelab_unifi_ap_client_count{ap_mac}``                     -- wireless client count per AP

   STAGE-007-015's vmalert reads the ``threshold`` label as its alert source of truth.

FAILURE / OK SEMANTICS:
- ``ctx.unifi is None`` -> ok=False, errors=["unifi client not configured"], no emits.
- ``stat_sta()`` returns UnifiError -> HARD FAIL (ok=False, errors=[msg]): stat/sta
  is the ONLY data source here, so a failure leaves nothing to emit.
- A 200-with-malformed-body (payload not dict, or data not a list) -> records=[],
  ok=True: the six drop gauges are still written (value 0.0) and the rollups emit at
  0.0 (no AP series). Empty real records behave identically.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, Final, cast

from homelab_monitor.kernel.config import load_cardinality_caps_config
from homelab_monitor.kernel.metrics.cardinality import CappedEmitter
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorEvent, CollectorResult
from homelab_monitor.kernel.unifi.errors import UnifiError
from homelab_monitor.plugins.collectors.integrations.unifi._parsing import (
    as_bool,
    as_float,
)

# --- Cap-config family key (matches _DEFAULT_CARDINALITY_FAMILIES, cap=200) -----
_CAP_FAMILY: Final[str] = "unifi_client_stats"

# --- Capped per-client family names (all keyed by {mac} only, Decision D1) ------
M_SIGNAL_DBM: Final[str] = "homelab_unifi_client_signal_dbm"
M_TX_RATE_BPS: Final[str] = "homelab_unifi_client_tx_rate_bps"
M_RX_RATE_BPS: Final[str] = "homelab_unifi_client_rx_rate_bps"
M_UPTIME: Final[str] = "homelab_unifi_client_uptime"
M_TX_BYTES: Final[str] = "homelab_unifi_client_tx_bytes"
M_RX_BYTES: Final[str] = "homelab_unifi_client_rx_bytes"

# --- Bounded rollup gauge names -------------------------------------------------
M_POOR_SIGNAL: Final[str] = "homelab_unifi_clients_poor_signal"
M_POOR_SATISFACTION: Final[str] = "homelab_unifi_clients_poor_satisfaction"
M_HIGH_RETRIES: Final[str] = "homelab_unifi_clients_high_retries"
M_AP_CLIENT_COUNT: Final[str] = "homelab_unifi_ap_client_count"

# --- Experience-rollup thresholds (Decision B1) ---------------------------------
# Typical WiFi guidance: signal below -70 dBm is poor; a UniFi "satisfaction" score
# below 50 indicates a degraded experience; a TX retry ratio above 10% signals RF
# contention / a struggling link. These constants ALSO appear as the {threshold}
# label so STAGE-007-015's vmalert can read the boundary from the series itself
# (the label string is the vmalert source of truth).
SIGNAL_POOR_DBM: Final[float] = -70.0
SATISFACTION_POOR: Final[float] = 50.0
RETRIES_HIGH_PCT: Final[float] = 10.0

# String forms of the thresholds, used verbatim as the {threshold} label value.
# Defined separately to keep the label clean ("-70", not "-70.0").
SIGNAL_POOR_DBM_LABEL: Final[str] = "-70"
SATISFACTION_POOR_LABEL: Final[str] = "50"
RETRIES_HIGH_PCT_LABEL: Final[str] = "10"


def _parse_records(payload: object) -> list[dict[str, object]]:
    """Narrow a classic ``{"data":[...]}`` payload to a list of record dicts.

    Returns [] when the payload is not a dict, ``data`` is not a list, or there are
    no dict entries. Non-dict entries are skipped.
    """
    if not isinstance(payload, dict):
        return []
    payload_dict = cast("dict[str, object]", payload)
    data_obj = payload_dict.get("data")
    if not isinstance(data_obj, list):
        return []
    data = cast("list[object]", data_obj)
    return [cast("dict[str, object]", r) for r in data if isinstance(r, dict)]


def _str_field(rec: dict[str, object], key: str) -> str | None:
    """Return rec[key] only if it is a str, else None."""
    val = rec.get(key)
    return val if isinstance(val, str) else None


class _Built:
    """Per-tick observation lists + rollup accumulators, built in one pass."""

    __slots__ = (
        "ap_counts",
        "high_retries_count",
        "poor_satisfaction_count",
        "poor_signal_count",
        "rx_bytes_obs",
        "rx_rate_obs",
        "signal_obs",
        "tx_bytes_obs",
        "tx_rate_obs",
        "uptime_obs",
    )

    def __init__(self) -> None:
        """Initialise empty observation lists, zero counts, empty AP map."""
        self.signal_obs: list[tuple[dict[str, str], float]] = []
        self.tx_rate_obs: list[tuple[dict[str, str], float]] = []
        self.rx_rate_obs: list[tuple[dict[str, str], float]] = []
        self.uptime_obs: list[tuple[dict[str, str], float]] = []
        self.tx_bytes_obs: list[tuple[dict[str, str], float]] = []
        self.rx_bytes_obs: list[tuple[dict[str, str], float]] = []
        self.poor_signal_count: int = 0
        self.poor_satisfaction_count: int = 0
        self.high_retries_count: int = 0
        self.ap_counts: dict[str, int] = {}


def _accumulate_rollups(built: _Built, rec: dict[str, object], *, is_wired: bool) -> None:
    """Update the four experience-rollup accumulators for one record.

    poor_satisfaction is evaluated for ALL clients (wired satisfaction is 100, never
    < 50). poor_signal / high_retries / ap_counts are wireless-only.
    """
    satisfaction = as_float(rec.get("satisfaction"))
    if satisfaction is not None and satisfaction < SATISFACTION_POOR:
        built.poor_satisfaction_count += 1

    if is_wired:
        return

    signal = as_float(rec.get("signal"))
    if signal is not None and signal < SIGNAL_POOR_DBM:
        built.poor_signal_count += 1

    attempts = as_float(rec.get("wifi_tx_attempts"))
    retries = as_float(rec.get("tx_retries"))
    # The retry ratio is a lifetime-cumulative historical average (not a current rate).
    # Guard attempts > 0 before dividing (avoids div-by-zero); skip otherwise.
    if (
        attempts is not None
        and retries is not None
        and attempts > 0
        and 100.0 * retries / attempts > RETRIES_HIGH_PCT
    ):
        built.high_retries_count += 1

    ap_mac = _str_field(rec, "ap_mac")
    if ap_mac is not None:
        built.ap_counts[ap_mac] = built.ap_counts.get(ap_mac, 0) + 1


def _accumulate_observations(
    built: _Built, rec: dict[str, object], labels: dict[str, str], *, is_wired: bool
) -> None:
    """Append per-client capped-family observations for one record.

    Wireless-only: signal, tx_rate (KBPS x1000 -> bps), rx_rate (KBPS x1000 -> bps).
    All clients: uptime, tx_bytes, rx_bytes (bytes keys depend on the wired flag).
    """
    if not is_wired:
        signal = as_float(rec.get("signal"))
        if signal is not None:
            built.signal_obs.append((labels, signal))
        tx_rate = as_float(rec.get("tx_rate"))
        if tx_rate is not None:
            built.tx_rate_obs.append((labels, tx_rate * 1000.0))  # KBPS -> bps (C1)
        rx_rate = as_float(rec.get("rx_rate"))
        if rx_rate is not None:
            built.rx_rate_obs.append((labels, rx_rate * 1000.0))  # KBPS -> bps (C1)

    uptime = as_float(rec.get("uptime"))
    if uptime is not None:
        built.uptime_obs.append((labels, uptime))

    tx_key = "wired-tx_bytes" if is_wired else "tx_bytes"
    rx_key = "wired-rx_bytes" if is_wired else "rx_bytes"
    tx_bytes = as_float(rec.get(tx_key))
    if tx_bytes is not None:
        built.tx_bytes_obs.append((labels, tx_bytes))
    rx_bytes = as_float(rec.get(rx_key))
    if rx_bytes is not None:
        built.rx_bytes_obs.append((labels, rx_bytes))


def _build(records: list[dict[str, object]]) -> _Built:
    """Single pass over records -> 6 observation lists + 4 rollup accumulators.

    Records with a missing/non-str ``mac`` are skipped entirely (no observation,
    no rollup contribution). ``labels`` is ``{mac}`` ONLY (Decision D1).
    """
    built = _Built()
    for rec in records:
        mac = _str_field(rec, "mac")
        if mac is None:
            continue
        labels = {"mac": mac}
        is_wired = as_bool(rec.get("is_wired"))
        _accumulate_observations(built, rec, labels, is_wired=is_wired)
        _accumulate_rollups(built, rec, is_wired=is_wired)
    return built


def _emit_capped(emitter: CappedEmitter, cap: int, built: _Built, emitted: list[int]) -> None:
    """Emit all six capped families through the SAME emitter (shared events list).

    ``emit_family`` returns survivor count and ALSO writes the per-family
    drop gauge -- so each call contributes ``survivors + 1`` to emitted[0].
    All six families use the identical {mac}-only label set -> the survivor-mac
    set is identical across families that share the same candidate set
    (Decision A2): wireless-only families agree with each other, all-client
    families agree with each other, and a wired mac never appears in a
    wireless-only family. (Across the two candidate sets the survivor sets are
    not guaranteed equal -- the capper sorts by mac and slices.)
    """
    emitted[0] += emitter.emit_family(M_SIGNAL_DBM, cap, built.signal_obs) + 1
    emitted[0] += emitter.emit_family(M_TX_RATE_BPS, cap, built.tx_rate_obs) + 1
    emitted[0] += emitter.emit_family(M_RX_RATE_BPS, cap, built.rx_rate_obs) + 1
    emitted[0] += emitter.emit_family(M_UPTIME, cap, built.uptime_obs) + 1
    emitted[0] += emitter.emit_family(M_TX_BYTES, cap, built.tx_bytes_obs) + 1
    emitted[0] += emitter.emit_family(M_RX_BYTES, cap, built.rx_bytes_obs) + 1


def _emit_rollups(ctx: CollectorContext, built: _Built, emitted: list[int]) -> None:
    """Emit the four bounded experience rollups (NOT capped)."""
    ctx.vm.write_gauge(
        M_POOR_SIGNAL, float(built.poor_signal_count), {"threshold": SIGNAL_POOR_DBM_LABEL}
    )
    emitted[0] += 1
    ctx.vm.write_gauge(
        M_POOR_SATISFACTION,
        float(built.poor_satisfaction_count),
        {"threshold": SATISFACTION_POOR_LABEL},
    )
    emitted[0] += 1
    ctx.vm.write_gauge(
        M_HIGH_RETRIES, float(built.high_retries_count), {"threshold": RETRIES_HIGH_PCT_LABEL}
    )
    emitted[0] += 1
    for ap_mac, count in built.ap_counts.items():
        ctx.vm.write_gauge(M_AP_CLIENT_COUNT, float(count), {"ap_mac": ap_mac})
        emitted[0] += 1


class UnifiClientStatsCollector(BaseCollector):
    """Emit per-client capped WiFi stats + bounded experience rollups from stat/sta.

    Reads classic ``stat/sta`` once per 60s tick. Builds six {mac}-keyed capped
    metric families (signal / tx-rate / rx-rate / uptime / tx-bytes / rx-bytes) and
    four bounded rollup gauges (poor-signal / poor-satisfaction / high-retries /
    per-AP client count).
    """

    name: ClassVar[str] = "unifi_client_stats"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "unifi"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll stat/sta, emit the six capped families + four rollups."""
        start = time.monotonic()
        if ctx.unifi is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["unifi client not configured"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        result = await ctx.unifi.stat_sta()
        if isinstance(result, UnifiError):
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=[result.message],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        events: list[CollectorEvent] = []
        emitted = [0]

        ctx.vm.write_gauge(
            "homelab_unifi_api_took_seconds",
            result.took_seconds,
            {"endpoint": result.endpoint},
        )
        emitted[0] += 1

        records = _parse_records(result.payload)
        caps = load_cardinality_caps_config()
        cap = caps.cap_for(_CAP_FAMILY)

        built = _build(records)

        emitter = CappedEmitter(writer=ctx.vm, events=events)
        _emit_capped(emitter, cap, built, emitted)
        _emit_rollups(ctx, built, emitted)

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted[0],
            errors=[],
            events=events,
            duration_seconds=time.monotonic() - start,
        )
