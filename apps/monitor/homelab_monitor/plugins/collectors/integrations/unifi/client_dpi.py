"""unifi_client_dpi collector -- per-client per-app DPI byte breakdown.

Polls the UniFi v2 traffic endpoint once per 5-minute tick (epoch-ms window, 24h
lookback) and emits a single BY-VOLUME cardinality-capped metric family:

- ``homelab_unifi_client_dpi_bytes{client,app,cat}`` -- a GAUGE of the ``total_bytes``
  per ``(client mac, app name, cat name)`` from the v2 traffic endpoint. Combined rx+tx
  cumulative bytes (see Value Semantics, below). No ``dir`` label (matches the DPI
  card's exact label set). Cumulative-as-gauge mirrors the other unifi byte families;
  PromQL ``increase()`` / ``rate()`` computes deltas downstream.

VALUE SEMANTICS:
The v2 API returns each app row with ``total_bytes`` (tx + rx combined). If present
and parseable, use it. Otherwise fall back to ``bytes_received + bytes_transmitted``.
If all are absent/unparseable, skip the row. (The production UniFi firmware 10.4.57
always supplies ``total_bytes``.)

APP/CATEGORY ID → NAME RESOLUTION:
Numeric ``application`` / ``category`` IDs from the v2 API are resolved to human
names via the bundled best-effort catalog (dpi_catalog.py). When an ID is absent
from the catalog, fall back to the raw stringified ID (e.g. "9999"). This fallback
MUST work (never crash, never blank).

Plus three always-emitted graceful-degrade indicators:

- ``homelab_unifi_dpi_enabled`` -- 1.0 when the v2 traffic endpoint was reached
  successfully (a successful fetch IS the reachability signal). The None / UnifiError
  paths return ok=False BEFORE this point, so this is always 1.0 when we reach the emit.
- ``homelab_unifi_dpi_client_records`` -- honest data signal: count of clients
  contributing ≥1 observation (i.e. with ≥1 usage row that survived parsing). When
  zero, DPI is reachable but has no data (empty-200 case). When positive, data is
  actually flowing. Lets dashboards distinguish "reachable but no data" from
  "producing data".
- ``homelab_unifi_api_took_seconds{endpoint="v2/traffic"}`` -- the API latency.

CARDINALITY CAP (deliberate, documented deviation from client_stats):
client_stats uses ``CappedEmitter.emit_family``, which sorts candidates
LEXICALLY by ``tuple(sorted(labels.items()))`` and slices -- it keeps the
lexically-smallest label tuples, NOT the biggest consumers. The DPI card
requires "top-N clients x top-N apps" = BY VOLUME. So this collector does its
OWN top-N-by-bytes selection: sort observations by combined-bytes descending
(stable lexical tiebreak), keep the top ``cap_for("unifi_dpi")`` (=100), emit
survivors, then emit the true ``homelab_metric_family_dropped_series`` gauge and
append ONE warning SuggestionEvent when dropped > 0 -- preserving emit_family's
drop-accounting honesty. We do NOT pre-truncate then call emit_family (that would
silently report dropped=0).

FAILURE / OK SEMANTICS:
- ``ctx.unifi is None`` -> ok=False, errors=["unifi client not configured"], no emits.
- ``v2_traffic()`` returns UnifiError -> ok=False, errors=[msg], no emits.
- A 200-with-malformed-body (payload not a dict, or client_usage_by_app not a
  list) -> records=[], ok=True: latency + dpi_enabled + dpi_client_records(=0) +
  drop gauge (=0) emitted, no data series.
- Empty data (``{"client_usage_by_app":[]}`` or all entries skipped) -> ok=True,
  same as above (dpi_client_records=0, no series).
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, Final, cast

from homelab_monitor.kernel.config import load_cardinality_caps_config
from homelab_monitor.kernel.metrics.cardinality import M_FAMILY_DROPPED_SERIES
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorEvent, CollectorResult, SuggestionEvent
from homelab_monitor.kernel.unifi.errors import UnifiError
from homelab_monitor.plugins.collectors.integrations.unifi._parsing import as_float
from homelab_monitor.plugins.collectors.integrations.unifi.dpi_catalog import (
    resolve_app,
    resolve_cat,
)

# --- Cap-config family key (matches _DEFAULT_CARDINALITY_FAMILIES, cap=100) ------
_CAP_FAMILY: Final[str] = "unifi_dpi"

# --- Metric names ---------------------------------------------------------------
M_CLIENT_DPI_BYTES: Final[str] = "homelab_unifi_client_dpi_bytes"
M_DPI_ENABLED: Final[str] = "homelab_unifi_dpi_enabled"
M_DPI_CLIENT_RECORDS: Final[str] = "homelab_unifi_dpi_client_records"

# --- Stable latency-endpoint label and time window ---------------------
_ENDPOINT_LABEL: Final[str] = "v2/traffic"
_WINDOW_MS: Final[int] = 24 * 60 * 60 * 1000  # 24h lookback window


def _now_ms() -> int:
    """Current unix time in milliseconds (v2 traffic requires epoch-MS bounds)."""
    return int(time.time() * 1000)


def _parse_clients(payload: object) -> list[dict[str, object]]:
    """Narrow a v2 traffic payload to its ``client_usage_by_app`` list of dicts.

    Returns [] when payload is not a dict, ``client_usage_by_app`` is missing/not a
    list, or it contains no dict entries. Non-dict entries are skipped.
    """
    if not isinstance(payload, dict):
        return []
    payload_dict = cast("dict[str, object]", payload)
    clients_obj = payload_dict.get("client_usage_by_app")
    if not isinstance(clients_obj, list):
        return []
    clients = cast("list[object]", clients_obj)
    return [cast("dict[str, object]", c) for c in clients if isinstance(c, dict)]


def _row_value(row: dict[str, object]) -> float | None:
    """Per-app byte value: total_bytes if parseable, else rx+tx, else None.

    Falls back to ``bytes_received + bytes_transmitted`` only when ``total_bytes`` is
    absent/unparseable. Returns None when no usable bytes are present (skip the row).
    """
    total = as_float(row.get("total_bytes"))
    if total is not None:
        return total
    rx = as_float(row.get("bytes_received"))
    tx = as_float(row.get("bytes_transmitted"))
    if rx is None and tx is None:
        return None
    return (rx or 0.0) + (tx or 0.0)


def _build_observations(
    clients: list[dict[str, object]],
) -> tuple[list[tuple[dict[str, str], float]], int]:
    """Build (labels, total_bytes) observations from v2 client_usage_by_app entries.

    v2 entry shape:
        {"client": {"mac": <str>, ...},
         "usage_by_app": [{"application": <int>, "category": <int>,
                           "total_bytes": <int>, "bytes_received": <int>,
                           "bytes_transmitted": <int>}, ...]}

    Resolution: app label = resolve_app(category, application); cat label =
    resolve_cat(category) — each falls back to the raw stringified id. Value =
    total_bytes when parseable, else bytes_received + bytes_transmitted, else skip.

    Returns (observations, client_record_count) where client_record_count is the
    number of clients that contributed at least one observation.

    Skip rules (each is a coverage FALSE branch):
      - ``client`` not a dict -> skip the entry.
      - client ``mac`` missing/not a non-empty str -> skip the entry.
      - ``usage_by_app`` not a list -> skip the entry.
      - a usage row not a dict -> skip the row.
      - a usage row's ``application``/``category`` not an int -> skip the row.
      - a usage row with no usable bytes (total_bytes AND rx AND tx unparseable) -> skip the row.
    """
    observations: list[tuple[dict[str, str], float]] = []
    client_records = 0
    for entry in clients:
        client_obj = entry.get("client")
        if not isinstance(client_obj, dict):
            continue
        client = cast("dict[str, object]", client_obj)
        mac = client.get("mac")
        if not isinstance(mac, str) or not mac:
            continue
        usage_obj = entry.get("usage_by_app")
        if not isinstance(usage_obj, list):
            continue
        usage = cast("list[object]", usage_obj)
        emitted_for_client = 0
        for row_obj in usage:
            if not isinstance(row_obj, dict):
                continue
            row = cast("dict[str, object]", row_obj)
            application = row.get("application")
            category = row.get("category")
            if not isinstance(application, int) or not isinstance(category, int):
                continue
            value = _row_value(row)
            if value is None:
                continue
            labels: dict[str, str] = {
                "client": mac,
                "app": resolve_app(category, application),
                "cat": resolve_cat(category),
            }
            observations.append((labels, value))
            emitted_for_client += 1
        if emitted_for_client > 0:
            client_records += 1
    return observations, client_records


class UnifiClientDpiCollector(BaseCollector):
    """Emit BY-VOLUME-capped per-client per-app DPI bytes from v2 traffic.

    Reads v2 traffic endpoint once per 5-minute tick (24h epoch-ms window).
    Builds ``{client,app,cat}``-keyed observations of total bytes (resolved
    names), keeps the top ``cap_for("unifi_dpi")`` BY VOLUME, and emits them as
    gauges alongside the dpi-enabled indicator, the data-signal record count,
    the API latency, and the true dropped-series gauge (+ one SuggestionEvent
    when over cap).
    """

    name: ClassVar[str] = "unifi_client_dpi"
    interval: ClassVar[timedelta] = timedelta(seconds=300)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "unifi"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll v2 traffic, emit the by-volume-capped DPI family + indicators."""
        start = time.monotonic()
        if ctx.unifi is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["unifi client not configured"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        end_ms = _now_ms()
        start_ms = end_ms - _WINDOW_MS
        resp = await ctx.unifi.v2_traffic(start_ms, end_ms)
        if isinstance(resp, UnifiError):
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=[resp.message],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        events: list[CollectorEvent] = []
        emitted = 0

        # Always emit the API latency gauge (graceful-degrade indicator).
        ctx.vm.write_gauge(
            "homelab_unifi_api_took_seconds", resp.took_seconds, {"endpoint": _ENDPOINT_LABEL}
        )
        emitted += 1

        # Always emit dpi_enabled = 1.0: we reached v2 traffic successfully, so DPI
        # is reachable. The None / UnifiError paths returned ok=False before this point.
        ctx.vm.write_gauge(M_DPI_ENABLED, 1.0, {})
        emitted += 1

        clients = _parse_clients(resp.payload)
        observations, client_records = _build_observations(clients)

        # Emit the honest data signal (client record count).
        ctx.vm.write_gauge(M_DPI_CLIENT_RECORDS, float(client_records), {})
        emitted += 1

        cap = max(0, load_cardinality_caps_config().cap_for(_CAP_FAMILY))
        # BY-VOLUME cap (top-N by total bytes), NOT lexical. Stable secondary
        # sort on the sorted label tuple gives deterministic tie-breaks.
        sorted_obs = sorted(observations, key=lambda o: (-o[1], tuple(sorted(o[0].items()))))
        survivors = sorted_obs[:cap]
        dropped = max(0, len(observations) - cap)

        for labels, value in survivors:
            ctx.vm.write_gauge(M_CLIENT_DPI_BYTES, value, labels)
            emitted += 1

        # Always emit the drop gauge (even when dropped == 0) so a recovered family
        # reports 0 rather than going stale. Mirrors CappedEmitter.emit_family.
        ctx.vm.write_gauge(M_FAMILY_DROPPED_SERIES, float(dropped), {"family": M_CLIENT_DPI_BYTES})
        emitted += 1

        if dropped > 0:
            events.append(
                SuggestionEvent(
                    title=f"Metric family {M_CLIENT_DPI_BYTES} exceeded its cardinality cap",
                    body=(
                        f"metric {M_CLIENT_DPI_BYTES} exceeded its "
                        f"{cap}-series budget ({len(observations)} seen, {dropped} dropped); "
                        f"raise the cap or narrow the client filter."
                    ),
                    severity="warning",
                )
            )

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted,
            errors=[],
            events=events,
            duration_seconds=time.monotonic() - start,
        )
