"""unifi_client_dpi collector -- per-client per-app DPI byte breakdown.

Polls classic ``stat/stadpi`` once per 5-minute tick and emits a single
BY-VOLUME cardinality-capped metric family:

- ``homelab_unifi_client_dpi_bytes{client,app,cat}`` -- a GAUGE of the COMBINED
  cumulative ``rx_bytes + tx_bytes`` per ``(client mac, app id, cat id)``. No
  ``dir`` label (matches the DPI card's exact label set). Cumulative-as-gauge
  mirrors the other unifi byte families; PromQL ``increase()`` / ``rate()``
  computes deltas downstream.

Plus two always-emitted graceful-degrade indicators:

- ``homelab_unifi_dpi_enabled`` -- 1.0 when the ``stat/stadpi`` endpoint was
  reached successfully (a successful fetch IS the reachability signal; we make
  ONE fetch and do NOT call ``get/setting/dpi`` separately). The None /
  UnifiError paths return ok=False BEFORE this point, so this is always 1.0 when
  we reach the emit.
- ``homelab_unifi_api_took_seconds{endpoint="stat/stadpi"}`` -- the API latency.

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
- ``stat_stadpi()`` returns UnifiError -> ok=False, errors=[msg], no emits.
- A 200-with-malformed-body (payload not a dict, or data not a list) -> records=[],
  ok=True: latency + dpi_enabled + drop gauge (=0) emitted, no data series.
- Empty data (``[]``) or empty-object sentinel (``[{}]``) -> ok=True, same as above.
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

# --- Cap-config family key (matches _DEFAULT_CARDINALITY_FAMILIES, cap=100) ------
_CAP_FAMILY: Final[str] = "unifi_dpi"

# --- Metric names ---------------------------------------------------------------
M_CLIENT_DPI_BYTES: Final[str] = "homelab_unifi_client_dpi_bytes"
M_DPI_ENABLED: Final[str] = "homelab_unifi_dpi_enabled"

# --- Stable latency-endpoint label ----------------------------------------------
_ENDPOINT_LABEL: Final[str] = "stat/stadpi"


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


def _build_observations(
    records: list[dict[str, object]],
) -> list[tuple[dict[str, str], float]]:
    """Build (labels, combined_bytes) observations from per-client DPI records.

    Per-client entry shape (Decision 1A):
        {"mac": <client mac>, "by_app": [{app:<int>, cat:<int>,
                                          rx_bytes:<int>, tx_bytes:<int>}, ...]}
    ``app`` / ``cat`` are NUMERIC ids stringified into labels. Combined value is
    ``(rx or 0.0) + (tx or 0.0)``. Skip rules (each is a coverage FALSE branch):
      - record ``mac`` missing or not a non-empty str -> skip the record.
      - record ``by_app`` not a list -> skip the record.
      - by_app entry not a dict -> skip the entry.
      - by_app entry missing ``app`` or ``cat`` -> skip the entry.
      - by_app entry with BOTH rx_bytes and tx_bytes unparseable -> skip the entry.
    """
    observations: list[tuple[dict[str, str], float]] = []
    for record in records:
        mac = record.get("mac")
        if not isinstance(mac, str) or not mac:
            continue
        by_app_obj = record.get("by_app")
        if not isinstance(by_app_obj, list):
            continue
        by_app = cast("list[object]", by_app_obj)
        for entry_obj in by_app:
            if not isinstance(entry_obj, dict):
                continue
            entry = cast("dict[str, object]", entry_obj)
            app = entry.get("app")
            cat = entry.get("cat")
            if app is None or cat is None:
                continue
            rx = as_float(entry.get("rx_bytes"))
            tx = as_float(entry.get("tx_bytes"))
            if rx is None and tx is None:
                continue
            total = (rx or 0.0) + (tx or 0.0)
            labels: dict[str, str] = {"client": mac, "app": str(app), "cat": str(cat)}
            observations.append((labels, total))
    return observations


class UnifiClientDpiCollector(BaseCollector):
    """Emit BY-VOLUME-capped per-client per-app DPI bytes from stat/stadpi.

    Reads classic ``stat/stadpi`` once per 5-minute tick. Builds
    ``{client,app,cat}``-keyed observations of combined cumulative rx+tx bytes,
    keeps the top ``cap_for("unifi_dpi")`` BY VOLUME, and emits them as gauges
    alongside the dpi-enabled indicator, the API latency, and the true
    dropped-series gauge (+ one SuggestionEvent when over cap).
    """

    name: ClassVar[str] = "unifi_client_dpi"
    interval: ClassVar[timedelta] = timedelta(seconds=300)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "unifi"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll stat/stadpi, emit the by-volume-capped DPI family + indicators."""
        start = time.monotonic()
        if ctx.unifi is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["unifi client not configured"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        resp = await ctx.unifi.stat_stadpi()
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

        # Always emit dpi_enabled = 1.0: we reached stat/stadpi successfully, so DPI
        # is reachable. We make ONE fetch and do NOT call get/setting/dpi separately;
        # a successful stat/stadpi IS the reachability signal. The None / UnifiError
        # paths returned ok=False before this point.
        ctx.vm.write_gauge(M_DPI_ENABLED, 1.0, {})
        emitted += 1

        records = _parse_records(resp.payload)
        observations = _build_observations(records)

        cap = max(0, load_cardinality_caps_config().cap_for(_CAP_FAMILY))
        # BY-VOLUME cap (top-N by combined bytes), NOT lexical. Stable secondary
        # sort on the sorted label tuple gives deterministic tie-breaks.
        sorted_obs = sorted(observations, key=lambda o: (-o[1], tuple(sorted(o[0].items()))))
        survivors = sorted_obs[:cap]
        dropped = max(0, len(observations) - cap)

        # TODO(STAGE-007-015): per-counter spike clamp lives in vmalert/PromQL, not
        # here -- collector emits raw cumulative bytes.
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
