"""unifi_threat collector -- active IDS/IPS alarm counts from rest/alarm.

Polls the classic ``rest/alarm?archived=false`` endpoint once per 60s tick and
emits:

- ``homelab_unifi_threat_count`` -- an ALWAYS-PRESENT GAUGE (no labels) of the
  total number of DISTINCT active alarms (deduped by ``_id``). It is emitted as
  ``0.0`` when there are no alarms, so the series is never absent -- this avoids
  the ``absent()`` trap that bit STAGE-007-015.
- ``homelab_unifi_threat{type=...}`` -- a per-type GAUGE of the distinct alarm
  count for each type PRESENT in this poll. Only present types emit a series; no
  zero-series is written for absent types (the always-present total handles the
  "all clear" signal).
- ``homelab_unifi_api_took_seconds{endpoint="rest/alarm?archived=false"}`` --
  the API latency (always emitted on a successful fetch).

The ``type`` label is derived per record via the fallback chain
``key`` -> ``subsystem`` -> ``"unknown"`` (see ``_threat_type``).

DEDUP: counting is stateless and within-poll. We keep a ``set`` of seen ``_id``
strings and a per-type ``set`` of ``_id`` strings. Using sets keyed by ``_id``
makes the count defensive against a duplicate record sharing the same ``_id``
(it is counted once). A record with a missing / empty / non-string ``_id`` is
skipped.

NO cardinality cap (the alarm-type space is tiny and bounded by the UDM's IDS/IPS
categories). NO SuggestionEvents.

FAILURE / OK SEMANTICS (mirrors the sibling collectors):
- ``ctx.unifi is None`` -> ok=False, errors=["unifi client not configured"], no emits.
- ``rest_alarm()`` returns UnifiError -> ok=False, errors=[message], no emits.
- A 200-with-malformed-body (payload not a dict, or data not a list) -> records=[],
  ok=True: latency + threat_count=0.0 emitted, no per-type series.
- Empty data (``[]``) -> ok=True, same as above.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, Final, cast

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorResult
from homelab_monitor.kernel.unifi.errors import UnifiError

# --- Metric names ---------------------------------------------------------------
M_THREAT_COUNT: Final[str] = "homelab_unifi_threat_count"
M_THREAT: Final[str] = "homelab_unifi_threat"


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


def _threat_type(record: dict[str, object]) -> str:
    """Derive the alarm type label via the fallback chain key -> subsystem -> unknown.

    Tries ``key`` first (a non-empty string), then ``subsystem`` (a non-empty
    string), else returns ``"unknown"``. Each branch is a distinct coverage path.
    """
    k = record.get("key")
    if isinstance(k, str) and k:
        return k
    s = record.get("subsystem")
    if isinstance(s, str) and s:
        return s
    return "unknown"


class UnifiAlarmsCollector(BaseCollector):
    """Emit active IDS/IPS alarm counts (total + per-type) from rest/alarm.

    Reads classic ``rest/alarm?archived=false`` once per 60s tick, dedups alarms
    by ``_id``, and emits an always-present total count plus a per-type breakdown
    for each type present in the poll.
    """

    name: ClassVar[str] = "unifi_threat"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "unifi"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll rest/alarm and emit the total + per-type threat counts."""
        start = time.monotonic()
        if ctx.unifi is None:
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=["unifi client not configured"],
                duration_seconds=time.monotonic() - start,
            )

        resp = await ctx.unifi.rest_alarm()
        if isinstance(resp, UnifiError):
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=[resp.message],
                duration_seconds=time.monotonic() - start,
            )

        emitted = 0

        # Always emit the API latency gauge (graceful-degrade indicator).
        ctx.vm.write_gauge(
            "homelab_unifi_api_took_seconds",
            resp.took_seconds,
            {"endpoint": resp.endpoint},
        )
        emitted += 1

        records = _parse_records(resp.payload)

        # Stateless within-poll distinct counting, keyed by _id (defensive dedup).
        seen_ids: set[str] = set()
        per_type: dict[str, set[str]] = {}
        for record in records:
            rid = record.get("_id")
            # Skip records with no usable id (missing / empty / non-string).
            if not (isinstance(rid, str) and rid):
                continue
            # Defensive dedup: a duplicate _id is counted once.
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            t = _threat_type(record)
            per_type.setdefault(t, set()).add(rid)

        total_count = len(seen_ids)

        # Always emit the total (0.0 when no alarms) with NO labels, so the series
        # is never absent (avoids the absent() trap from STAGE-007-015).
        ctx.vm.write_gauge(M_THREAT_COUNT, float(total_count), {})
        emitted += 1

        # Per-type breakdown -- only types present in this poll (no zero-series).
        for t, ids in per_type.items():
            ctx.vm.write_gauge(M_THREAT, float(len(ids)), {"type": t})
            emitted += 1

        return CollectorResult(
            ok=True,
            metrics_emitted=emitted,
            errors=[],
            duration_seconds=time.monotonic() - start,
        )
