"""pihole_ftl_health collector — FTL process health + database stats.

Two-endpoint collector that polls Pi-hole v6 once per 60s across TWO endpoints
with PER-ENDPOINT resilience:

- ``GET /api/info/ftl``      -> FTL process uptime, CPU/memory usage, privacy level,
  and nested dnsmasq cache stats.
- ``GET /api/info/database`` -> SQLite database file size and on-disk query count.

PER-ENDPOINT RESILIENCE: a failure of ONE endpoint does NOT fail the run. The run
is ``ok=True`` if AT LEAST ONE endpoint call succeeded (not a PiholeError);
``ok=False`` only when BOTH endpoints error or ``ctx.pihole`` is None.

PAYLOAD SHAPE: Unlike gravity.py (which treats non-dict payloads as sub-skips that
keep the endpoint ok), this collector treats a non-dict payload as a genuine error:
the endpoint bool is set False and an error message is appended.

DNSMASQ METRICS: the nested ``dnsmasq`` object (cache insertions + live_freed) is
optional. If absent or not a dict, the two cache metrics are silently skipped
(no error, no ok=False).

METRICS:
- 2 api-took gauges            {endpoint="info/ftl"}, {endpoint="info/database"}
- 1 ftl-uptime gauge           {}
- 1 ftl-cpu gauge              {}
- 1 ftl-memory gauge           {}
- 1 privacy-level gauge        {}
- 2 dnsmasq cache gauges (if present) {1 insertions, 1 evictions}
- 1 db-size gauge              {}
- 1 db-queries gauge           {}
-> 10 metrics on a full healthy run (both endpoints OK, dnsmasq present).

SCAFFOLDING: feeds alert rules and Grafana dashboards (STAGE-026).
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
    emit_numeric,
)

# ---------------------------------------------------------------------------
# Metric name constants (PUBLIC — these are the literal-name contract)
# ---------------------------------------------------------------------------

M_API_TOOK = "homelab_pihole_api_took_seconds"
M_FTL_UPTIME = "homelab_pihole_ftl_uptime_seconds"
M_FTL_CPU = "homelab_pihole_ftl_cpu_percent"
M_FTL_MEMORY = "homelab_pihole_ftl_memory_percent"
M_PRIVACY_LEVEL = "homelab_pihole_privacy_level"
M_DNSMASQ_CACHE_INSERTIONS = "homelab_pihole_dnsmasq_cache_insertions"
M_DNSMASQ_CACHE_EVICTIONS = "homelab_pihole_dnsmasq_cache_evictions"
M_DB_SIZE = "homelab_pihole_db_size_bytes"
M_DB_QUERIES = "homelab_pihole_db_queries_total"


def _emit_ftl(
    ctx: CollectorContext,
    payload: dict[str, object],
    emitted: list[int],
) -> None:
    """Emit FTL process metrics from /api/info/ftl payload.

    Emits:
    - uptime (seconds)
    - %cpu (percent)
    - %mem (percent)
    - privacy_level (int)
    - dnsmasq.dns_cache_inserted (count, optional)
    - dnsmasq.dns_cache_live_freed (count, optional)

    The dnsmasq object is optional; if absent or non-dict, the cache metrics are
    silently skipped (no error).
    """
    ftl_obj = payload.get("ftl")
    if not isinstance(ftl_obj, dict):
        return
    ftl = cast("dict[str, object]", ftl_obj)
    emit_numeric(ctx, M_FTL_UPTIME, ftl.get("uptime"), {}, emitted)
    emit_numeric(ctx, M_FTL_CPU, ftl.get("%cpu"), {}, emitted)
    emit_numeric(ctx, M_FTL_MEMORY, ftl.get("%mem"), {}, emitted)
    emit_numeric(ctx, M_PRIVACY_LEVEL, ftl.get("privacy_level"), {}, emitted)
    dnsmasq_obj = ftl.get("dnsmasq")
    if isinstance(dnsmasq_obj, dict):
        dnsmasq = cast("dict[str, object]", dnsmasq_obj)
        emit_numeric(
            ctx,
            M_DNSMASQ_CACHE_INSERTIONS,
            dnsmasq.get("dns_cache_inserted"),
            {},
            emitted,
        )
        emit_numeric(
            ctx,
            M_DNSMASQ_CACHE_EVICTIONS,
            dnsmasq.get("dns_cache_live_freed"),
            {},
            emitted,
        )
    # else: dnsmasq absent or not a dict -> skip cache metrics (no error)


def _emit_database(
    ctx: CollectorContext,
    payload: dict[str, object],
    emitted: list[int],
) -> None:
    """Emit database metrics from /api/info/database payload.

    Emits:
    - size (bytes)
    - queries_disk (total on-disk queries)

    Note: reads queries_disk (on-disk total), NOT queries.
    """
    emit_numeric(ctx, M_DB_SIZE, payload.get("size"), {}, emitted)
    emit_numeric(ctx, M_DB_QUERIES, payload.get("queries_disk"), {}, emitted)


class PiholeFtlHealthCollector(BaseCollector):
    """Emit FTL process health + DB stats from /api/info/ftl and /api/info/database.

    Two-endpoint collector with per-endpoint resilience (mirrors gravity.py).
    Polls both endpoints at 60s interval. DB size changes slowly but 60s is
    harmless and avoids a separate scheduling concern.

    Metrics emitted on a healthy run:
    - 2 api-took gauges          {endpoint="info/ftl"}, {endpoint="info/database"}
    - 1 ftl-uptime gauge         {}
    - 1 ftl-cpu gauge            {}
    - 1 ftl-memory gauge         {}
    - 1 privacy-level gauge      {}
    - 1 dnsmasq-cache-insertions {}
    - 1 dnsmasq-cache-evictions  {}
    - 1 db-size gauge            {}
    - 1 db-queries gauge         {}
    -> metrics_emitted = 10 on a full happy-path run (assuming dnsmasq present).

    FAILURE SEMANTICS (per-endpoint resilience):
    - ctx.pihole is None -> ok=False, errors=["pihole client not configured"], 0 emits.
    - BOTH endpoints PiholeError -> ok=False, both error messages in errors, 0 emits.
    - ONE endpoint PiholeError -> ok=True (other succeeded), error appended.
    - endpoint OK but payload not a dict -> sets that endpoint's ok=False,
      appends "unexpected payload shape (info/ftl)" or "unexpected payload shape
      (info/database)"; api_took for that endpoint was ALREADY emitted before
      the shape check.
    """

    name: ClassVar[str] = "pihole_ftl_health"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "pihole"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll /api/info/ftl + /api/info/database, emit gauges, return CollectorResult."""
        start = time.monotonic()

        if ctx.pihole is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["pihole client not configured"],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        emitted: list[int] = [0]
        errors: list[str] = []
        ftl_ok = False
        db_ok = False

        # --- Endpoint 1: /api/info/ftl ---
        ftl_result = await ctx.pihole.info_ftl()
        if isinstance(ftl_result, PiholeError):
            errors.append(ftl_result.message)
        else:
            ctx.vm.write_gauge(
                M_API_TOOK, ftl_result.took_seconds, {"endpoint": ftl_result.endpoint}
            )
            emitted[0] += 1
            ftl_ok = True
            ftl_payload: object = ftl_result.payload
            if isinstance(ftl_payload, dict):
                _emit_ftl(ctx, cast("dict[str, object]", ftl_payload), emitted)
            else:
                ftl_ok = False
                errors.append("unexpected payload shape (info/ftl)")

        # --- Endpoint 2: /api/info/database ---
        db_result = await ctx.pihole.info_database()
        if isinstance(db_result, PiholeError):
            errors.append(db_result.message)
        else:
            ctx.vm.write_gauge(M_API_TOOK, db_result.took_seconds, {"endpoint": db_result.endpoint})
            emitted[0] += 1
            db_ok = True
            db_payload: object = db_result.payload
            if isinstance(db_payload, dict):
                _emit_database(ctx, cast("dict[str, object]", db_payload), emitted)
            else:
                db_ok = False
                errors.append("unexpected payload shape (info/database)")

        return CollectorResult(
            ok=ftl_ok or db_ok,
            metrics_emitted=emitted[0],
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )
