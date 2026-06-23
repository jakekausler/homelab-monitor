"""pihole_gravity collector — gravity domain count + per-adlist stats + derived age.

Two-endpoint collector (FIRST of its kind in this repo). Polls Pi-hole v6 once
per 30s across TWO endpoints with PER-ENDPOINT resilience:

- ``GET /api/info/ftl``  -> ``payload["ftl"]["database"]["gravity"]`` is the total
  number of domains currently on the gravity (block) list. This endpoint has NO
  timestamp anywhere.
- ``GET /api/lists``     -> a list of adlist entries, each with ``number`` (domains
  contributed), ``enabled`` (bool), ``status`` (int code), and ``date_updated``
  (epoch seconds, int).

DERIVED AGE: ``homelab_pihole_gravity_last_update_age_seconds`` is DERIVED from
``max(date_updated)`` across all adlists — it is NOT read from ``/api/info/ftl``
(which carries no timestamp). The epoch is converted with
``datetime.fromtimestamp(epoch, UTC)`` so there is NO timezone artifact and NO
ISO-parse step (unlike the HA staleness collector). The age is clamped >= 0 to
absorb clock skew (a future ``date_updated`` -> age 0.0). When NO adlist carries
a valid ``date_updated``, the age gauge is SKIPPED (we do NOT emit 0).

WINDOW SEMANTICS: ``gravity_domains`` and per-adlist ``number`` are instantaneous
counts (current gravity state), NOT 24h-rolling counters. Downstream consumers
MUST NOT apply ``rate()`` / ``increase()``.

STATUS CODES: Pi-hole adlist ``status`` is mapped to a stable name label via
``_STATUS_NAMES``; an unrecognized code becomes ``unknown_<code>``. This keeps
the alert contract readable (see STAGE-006-016: ``PiholeAdlistFailing`` matches
``homelab_pihole_adlist_status{status!="ok"} == 1``).

PER-ENDPOINT RESILIENCE: a failure of ONE endpoint does NOT fail the run. The
run is ``ok=True`` if AT LEAST ONE endpoint call succeeded (not a PiholeError);
``ok=False`` only when BOTH endpoints error or ``ctx.pihole`` is None. A
successful endpoint call whose payload is mis-shaped (e.g. missing ``gravity``
field) is a SUB-SKIP, not an endpoint failure — the endpoint bool stays True
because the call itself succeeded (api_took was emitted).

CARDINALITY: per-adlist series are capped at ``MAX_ADLISTS`` (defensive — a
homelab typically has < 10 adlists; the cap guards against a runaway list). The
``comment`` field is DELIBERATELY NOT a label (it is free text -> churn).

STAGE-006-016 CONTRACT (no TZ-guard needed because epoch):
- ``PiholeAdlistFailing``  := ``homelab_pihole_adlist_status{status!="ok"} == 1``
- ``PiholeGravityStale``   := on ``homelab_pihole_gravity_last_update_age_seconds``

SCAFFOLDING: metrics consumed by alert stage STAGE-006-016 + Grafana STAGE-026.
The api-took gauge feeds the API-latency alerting introduced in STAGE-006-016.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
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
M_GRAVITY_DOMAINS = "homelab_pihole_gravity_domains"
M_GRAVITY_AGE = "homelab_pihole_gravity_last_update_age_seconds"
M_ADLIST_DOMAINS = "homelab_pihole_adlist_domains"
M_ADLIST_ENABLED = "homelab_pihole_adlist_enabled"
M_ADLIST_STATUS = "homelab_pihole_adlist_status"

# Label key for the per-adlist identity (the adlist id, as a string).
_LIST_LABEL = "list"

# Pi-hole adlist status code -> stable name. Unknown codes -> "unknown_<code>".
_STATUS_NAMES: dict[int, str] = {
    0: "not_run",
    1: "ok",
    2: "download_failed",
    3: "parse_failed",
}

# Defensive cardinality cap on per-adlist series. A homelab has < 10 adlists;
# this guards against a runaway list (card requirement: "cap if huge adlist count").
MAX_ADLISTS = 200


def _status_name(code: int) -> str:
    """Map a Pi-hole adlist status code to its stable name (unknown -> unknown_<code>)."""
    return _STATUS_NAMES.get(code, f"unknown_{code}")


def _emit_gravity_domains(
    ctx: CollectorContext,
    payload: dict[str, object],
    emitted: list[int],
) -> None:
    """Emit homelab_pihole_gravity_domains from payload["ftl"]["database"]["gravity"].

    Sub-skips (no error, no crash) when the ``ftl`` or ``database`` sub-objects are
    missing / not dicts, or when ``gravity`` is non-numeric (emit_numeric handles
    the last case). All skip branches are required for 100% branch coverage.
    """
    ftl_obj = payload.get("ftl")
    if not isinstance(ftl_obj, dict):
        return
    ftl = cast("dict[str, object]", ftl_obj)
    database_obj = ftl.get("database")
    if not isinstance(database_obj, dict):
        return
    database = cast("dict[str, object]", database_obj)
    emit_numeric(ctx, M_GRAVITY_DOMAINS, database.get("gravity"), {}, emitted)


def _emit_adlist(
    ctx: CollectorContext,
    entry: object,
    emitted: list[int],
) -> float | None:
    """Emit the three per-adlist gauges for one /api/lists entry.

    Returns the entry's ``date_updated`` epoch (as float) for age aggregation, or
    None when the entry is skipped or has no valid ``date_updated``. All skip
    branches are required for 100% branch coverage.

    Emits (when present & well-typed):
    - homelab_pihole_adlist_domains {list, address}   <- number
    - homelab_pihole_adlist_enabled {list, address}   <- 1.0/0.0 from bool enabled
    - homelab_pihole_adlist_status  {list, address, status}  always value 1.0
    """
    # 1. Entry must be a dict.
    if not isinstance(entry, dict):
        return None
    e = cast("dict[str, object]", entry)

    # 2. id must be a real int (NOT bool) — it is the stable label identity.
    id_obj = e.get("id")
    if not isinstance(id_obj, int) or isinstance(id_obj, bool):
        return None
    list_label = str(id_obj)

    # 3. address is a descriptive label; default "" when absent/non-str so the
    #    label set stays consistent across entries.
    address_obj = e.get("address")
    address = address_obj if isinstance(address_obj, str) else ""

    base_labels = {_LIST_LABEL: list_label, "address": address}

    # 4. domains (number). emit_numeric skips a non-numeric / missing value.
    emit_numeric(ctx, M_ADLIST_DOMAINS, e.get("number"), base_labels, emitted)

    # 5. enabled (bool). Skip when non-bool.
    enabled_obj = e.get("enabled")
    if isinstance(enabled_obj, bool):
        ctx.vm.write_gauge(M_ADLIST_ENABLED, 1.0 if enabled_obj else 0.0, base_labels)
        emitted[0] += 1

    # 6. status (int, not bool). Skip when non-int. Value is always 1.0; the code
    #    name lives in the {status} label so PromQL can match status!="ok".
    status_obj = e.get("status")
    if isinstance(status_obj, int) and not isinstance(status_obj, bool):
        status_labels = {**base_labels, "status": _status_name(status_obj)}
        ctx.vm.write_gauge(M_ADLIST_STATUS, 1.0, status_labels)
        emitted[0] += 1

    # 7. date_updated -> age aggregation input. Return the epoch (float) or None.
    return as_float(e.get("date_updated"))


def _emit_gravity_age(
    ctx: CollectorContext,
    epochs: list[float],
    emitted: list[int],
) -> None:
    """Emit homelab_pihole_gravity_last_update_age_seconds from max(epochs).

    DERIVED age: now_utc - fromtimestamp(max(date_updated)), clamped >= 0. Skips
    entirely (does NOT emit 0) when ``epochs`` is empty (no valid date_updated).
    """
    if not epochs:
        return
    latest = max(epochs)
    now = datetime.now(UTC)
    ts = datetime.fromtimestamp(latest, UTC)
    age = max((now - ts).total_seconds(), 0.0)
    ctx.vm.write_gauge(M_GRAVITY_AGE, age, {})
    emitted[0] += 1


class PiholeGravityCollector(BaseCollector):
    """Emit gravity domain count + per-adlist stats + derived gravity-update age.

    Polls TWO endpoints per 30s with per-endpoint resilience. On a healthy run
    with the reference Pi-hole (5 adlists):
    - 2  api-took gauges       {endpoint="info/ftl"}, {endpoint="lists"}
    - 1  gravity-domains gauge
    - 5  adlist-domains gauges {list, address}
    - 5  adlist-enabled gauges {list, address}
    - 5  adlist-status gauges  {list, address, status}
    - 1  gravity-age gauge
    -> metrics_emitted = 19 on the full reference payload.

    FAILURE SEMANTICS (per-endpoint resilience):
    - ctx.pihole is None -> ok=False, errors=["pihole client not configured"], 0 emits.
    - BOTH endpoints PiholeError -> ok=False, errors=[ftl.msg, lists.msg], 0 emits.
    - ONE endpoint PiholeError -> ok=True (the other succeeded), error appended.
    - endpoint OK but payload mis-shaped -> sub-skips (endpoint bool stays True;
      its api_took was already emitted).
    """

    name: ClassVar[str] = "pihole_gravity"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "pihole"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll /api/info/ftl + /api/lists, emit gauges, return CollectorResult."""
        start = time.monotonic()

        # Guard: pihole client not configured.
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
        lists_ok = False

        # --- Endpoint 1: /api/info/ftl (gravity domain count) ---
        ftl_result = await ctx.pihole.info_ftl()
        if isinstance(ftl_result, PiholeError):
            errors.append(ftl_result.message)
        else:
            # The call succeeded -> endpoint bool is True regardless of payload shape.
            ctx.vm.write_gauge(
                M_API_TOOK, ftl_result.took_seconds, {"endpoint": ftl_result.endpoint}
            )
            emitted[0] += 1
            ftl_ok = True
            ftl_payload: object = ftl_result.payload
            if isinstance(ftl_payload, dict):
                _emit_gravity_domains(ctx, cast("dict[str, object]", ftl_payload), emitted)
            # else: payload not a dict -> gravity sub-skipped; ftl_ok stays True.

        # --- Endpoint 2: /api/lists (per-adlist stats + age aggregation) ---
        lists_result = await ctx.pihole.lists()
        if isinstance(lists_result, PiholeError):
            errors.append(lists_result.message)
        else:
            ctx.vm.write_gauge(
                M_API_TOOK, lists_result.took_seconds, {"endpoint": lists_result.endpoint}
            )
            emitted[0] += 1
            lists_ok = True
            lists_payload: object = lists_result.payload
            if isinstance(lists_payload, dict):
                lists_obj = cast("dict[str, object]", lists_payload).get("lists")
                if isinstance(lists_obj, list):
                    lists_seq = cast("list[object]", lists_obj)
                    epochs: list[float] = []
                    for entry in lists_seq[:MAX_ADLISTS]:
                        epoch = _emit_adlist(ctx, entry, emitted)
                        if epoch is not None:
                            epochs.append(epoch)
                    _emit_gravity_age(ctx, epochs, emitted)
                # else: "lists" missing / not a list -> no adlist/age; lists_ok stays True.
            # else: payload not a dict -> adlist/age sub-skipped; lists_ok stays True.

        # ok=True if AT LEAST ONE endpoint call succeeded (Design Decision B:
        # ok=False only if BOTH fail or ctx.pihole is None).
        return CollectorResult(
            ok=ftl_ok or lists_ok,
            metrics_emitted=emitted[0],
            errors=errors,
            events=[],
            duration_seconds=time.monotonic() - start,
        )
