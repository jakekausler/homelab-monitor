"""pihole_stats_summary collector — core query statistics from /api/stats/summary.

Polls Pi-hole v6 ``GET /api/stats/summary`` once per 30s and emits gauges for
top-line query counts, client counts, and the three enum families (query type,
query status, reply type).

WINDOW SEMANTICS: All emitted values are 24-hour ROLLING window aggregates as
reported by Pi-hole FTL. They are NOT monotonic counters — downstream consumers
MUST NOT apply ``rate()`` or ``increase()`` to these metrics. Treat them as
instantaneous snapshots of the 24h window.

INTENTIONALLY OMITTED:
- ``homelab_pihole_unique_clients`` — RETRACTED (STAGE-006-005): the
  ``/api/stats/summary`` endpoint does not include a ``unique_clients`` field in
  Pi-hole v6.6.2; the ``clients`` sub-object carries ``active`` and ``total``
  only.  No source exists for a unique-clients gauge at this endpoint.
- ``gravity.*`` metrics — OWNED by STAGE-006-007 (gravity/blocklist stats
  collector). The ``gravity`` sub-object in ``/api/stats/summary`` is
  intentionally skipped here to avoid duplication; STAGE-006-007 will emit
  ``homelab_pihole_gravity_domains_being_blocked`` and
  ``homelab_pihole_gravity_last_update``.

ENUM CARDINALITY: The three enum families (query type / status / reply) have
bounded, stable cardinality (16 / 20 / 15 labels respectively, defined by
Pi-hole FTL). No cardinality cap is applied.

SCAFFOLDING: metrics consumed by alert stages 016/017 + Grafana dashboard
STAGE-026. The api-took gauge feeds the API-latency alerting introduced in
STAGE-006-016.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, cast

from homelab_monitor.kernel.pihole.errors import PiholeError
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult
from homelab_monitor.plugins.collectors.integrations.pihole._parsing import (
    as_float,
    emit_numeric,
)

# ---------------------------------------------------------------------------
# Metric name constants
# ---------------------------------------------------------------------------

M_API_TOOK = "homelab_pihole_api_took_seconds"

# --- queries sub-object scalars ---
M_QUERIES_TOTAL = "homelab_pihole_queries_total"
M_QUERIES_BLOCKED = "homelab_pihole_blocked_total"
M_QUERIES_FORWARDED = "homelab_pihole_forwarded_total"
M_QUERIES_CACHED = "homelab_pihole_cached_total"
M_PERCENT_BLOCKED = "homelab_pihole_percent_blocked"
M_QUERY_FREQUENCY = "homelab_pihole_query_frequency"
M_UNIQUE_DOMAINS = "homelab_pihole_unique_domains"

# --- clients sub-object scalars ---
M_ACTIVE_CLIENTS = "homelab_pihole_active_clients"
M_TOTAL_CLIENTS = "homelab_pihole_total_clients"

# --- enum families ---
M_QUERY_BY_TYPE = "homelab_pihole_query_by_type"
M_QUERY_BY_STATUS = "homelab_pihole_query_by_status"
M_QUERY_BY_REPLY = "homelab_pihole_query_by_reply"


def _emit_enum(
    ctx: CollectorContext,
    metric_name: str,
    container: object,
    label_key: str,
    emitted: list[int],
) -> None:
    """Emit one gauge per entry in a {label: int} enum dict.

    ``container`` is expected to be a dict[str, numeric]. Silently skips:
    - ``container`` is None or not a dict (missing sub-object branch)
    - an individual entry whose value is non-numeric (as_float returns None)

    These branches are required for 100% branch coverage (see test cases
    ``test_emit_enum_container_missing`` and ``test_emit_enum_non_numeric_value``).
    """
    if not isinstance(container, dict):
        # Missing or wrong-type enum container — skip entire family; not an error.
        return
    enum_dict = cast("dict[str, object]", container)
    for label, value_obj in enum_dict.items():
        val = as_float(value_obj)
        if val is not None:
            ctx.vm.write_gauge(metric_name, val, {label_key: label})
            emitted[0] += 1


class PiholeStatsSummaryCollector(BaseCollector):
    """Emit core Pi-hole query statistics from GET /api/stats/summary.

    Polls once per 30 seconds. Emits:
    - 1  api-took gauge
    - 7  queries sub-object scalars (total, blocked, forwarded, cached,
         percent_blocked, frequency, unique_domains)
    - 2  clients sub-object scalars (active, total)
    - up to 16 query-by-type gauges  {type=<label>}
    - up to 20 query-by-status gauges {status=<label>}
    - up to 15 query-by-reply gauges  {reply=<label>}

    Total on a healthy full-payload run: 1 + 7 + 2 + 16 + 20 + 15 = 61
    (fewer when Pi-hole omits enum entries, which is normal for quiet installs).

    FAILURE SEMANTICS:
    - ctx.pihole is None → ok=False, errors=["pihole client not configured"], 0 emits.
    - stats_summary() returns PiholeError → ok=False, errors=[result.message], 0 emits.
    - payload not a dict → ok=False, errors=["unexpected payload shape"], 0 emits.
    - queries sub-object missing / not-a-dict → query scalars + enum families skipped;
      clients emitted if present; ok=True, partial result.
    - clients sub-object missing / not-a-dict → client scalars skipped; queries emitted
      if present; ok=True, partial result.
    - individual scalar non-numeric → that metric skipped; others continue.
    - enum sub-object (types / status / replies) missing / not-a-dict → that family
      skipped; others continue.
    - individual enum entry non-numeric → that entry skipped; others continue.
    """

    name: ClassVar[str] = "pihole_stats_summary"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "pihole"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll /api/stats/summary, emit gauges, return CollectorResult."""
        start = time.monotonic()

        # Guard: pihole client not configured
        if ctx.pihole is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["pihole client not configured"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        result = await ctx.pihole.stats_summary()

        # Guard: transport / auth / HTTP error
        if isinstance(result, PiholeError):
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=[result.message],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        # Guard: payload shape — must be a dict
        raw_payload: object = result.payload
        if not isinstance(raw_payload, dict):
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["unexpected payload shape"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        payload = cast("dict[str, object]", raw_payload)
        emitted: list[int] = [0]

        # --- api-took (always present when we have a successful response) ---
        ctx.vm.write_gauge(M_API_TOOK, result.took_seconds, {"endpoint": result.endpoint})
        emitted[0] += 1

        # --- queries sub-object ---
        queries_obj = payload.get("queries")
        if isinstance(queries_obj, dict):
            queries = cast("dict[str, object]", queries_obj)
            emit_numeric(ctx, M_QUERIES_TOTAL, queries.get("total"), {}, emitted)
            emit_numeric(ctx, M_QUERIES_BLOCKED, queries.get("blocked"), {}, emitted)
            emit_numeric(ctx, M_QUERIES_FORWARDED, queries.get("forwarded"), {}, emitted)
            emit_numeric(ctx, M_QUERIES_CACHED, queries.get("cached"), {}, emitted)
            emit_numeric(ctx, M_PERCENT_BLOCKED, queries.get("percent_blocked"), {}, emitted)
            emit_numeric(ctx, M_QUERY_FREQUENCY, queries.get("frequency"), {}, emitted)
            emit_numeric(ctx, M_UNIQUE_DOMAINS, queries.get("unique_domains"), {}, emitted)

            # enum families
            _emit_enum(ctx, M_QUERY_BY_TYPE, queries.get("types"), "type", emitted)
            _emit_enum(ctx, M_QUERY_BY_STATUS, queries.get("status"), "status", emitted)
            _emit_enum(ctx, M_QUERY_BY_REPLY, queries.get("replies"), "reply", emitted)
        # else: queries sub-object missing / not-a-dict — skip without error (partial result)

        # --- clients sub-object ---
        clients_obj = payload.get("clients")
        if isinstance(clients_obj, dict):
            clients = cast("dict[str, object]", clients_obj)
            emit_numeric(ctx, M_ACTIVE_CLIENTS, clients.get("active"), {}, emitted)
            emit_numeric(ctx, M_TOTAL_CLIENTS, clients.get("total"), {}, emitted)
        # else: clients sub-object missing / not-a-dict — skip without error (partial result)

        # gravity sub-object intentionally not read here — owned by STAGE-006-007.

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted[0],
            errors=[],
            events=[],
            duration_seconds=time.monotonic() - start,
        )
