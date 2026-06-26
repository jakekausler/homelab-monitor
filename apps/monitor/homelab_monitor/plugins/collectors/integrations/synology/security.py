"""synology_security collector — DSM Security Advisor posture + active-connection count.

EPIC-008 STAGE-008-013. Fetches TWO CO-EQUAL DSM APIs once per 1-hour tick:
  - SYNO.Core.SecurityScan.Status/system_get -> Security Advisor scan posture
  - SYNO.Core.CurrentConnection/list         -> active session list (COUNTED ONLY)

CO-EQUAL COMBINE (mirrors STAGE-008-012 updates.py): there is NO primary. ``_fetch``
records-and-continues on EITHER fetch's client error; the run is ok=False ONLY when
BOTH fetches fail (``ok = sec_resp is not None or conn_resp is not None``). A single-fetch
failure is a DEGRADED ok=True run. ``_emit`` ALWAYS runs. An unconfigured client is ok=False.

ALWAYS-EMIT BASELINES (alertable contract): ``security_status`` and ``security_safe`` ALWAYS
emit, even when the security fetch failed. They are seeded in ``_Built.__init__`` —
``security_status`` to 2.0 ("not clearly safe", conservative) and ``security_safe`` to 0.0.
A successful parse OVERWRITES them.

DELIBERATE SEED-0 BREAK — ``active_connections`` is EMIT-ON-SUCCESS, NOT a seeded
0-baseline. 0 connections is a legitimate value distinguishable from a failed fetch, and
this is a trend metric (not an alertable bad-state gauge). On a connection-fetch failure
OR an absent/non-numeric ``total`` it emits NOTHING (the family is absent that tick). This
intentionally diverges from the Wave-B always-emit-0 convention used by the security scalars.

CONNECTIONS FRESHNESS NOTE — the 1-hour cadence means ``active_connections`` is a coarse
trend. STAGE-008-027's connections panel re-queries on demand for a live count; this metric
is the historical series only.

PARSE GOTCHA — the security ``items`` value is a DICT KEYED BY CATEGORY (malware, network,
systemCheck, update, userInfo), NOT a list. Parse with ``as_dict(nested(payload, "items"))``
then ``nested(items_dict, cat, "fail", sev)``. NEVER ``as_list_of_dicts`` for security items.
The connection ``items`` IS a list but is NOT iterated (we read top-level ``total``).

CARDINALITY: every family is cap-routed through ``capped_emitter`` + ``cap_for_synology``
(default 500). ``metrics_emitted`` = sum of ``emit_family() + 1`` per family + the api_took
gauges from each successful fetch.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Final

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorEvent, CollectorResult
from homelab_monitor.kernel.synology.client import SynologyResponse
from homelab_monitor.kernel.synology.errors import SynologyError
from homelab_monitor.plugins.collectors.integrations.synology._shared import (
    as_dict,
    as_float,
    cap_for_synology,
    capped_emitter,
    client_unconfigured_result,
    fetch_or_result,
    nested,
)

# --- Metric family names
M_SECURITY_STATUS: Final[str] = "homelab_synology_security_status"
M_SECURITY_SAFE: Final[str] = "homelab_synology_security_safe"
M_SECURITY_FINDINGS: Final[str] = "homelab_synology_security_findings"
M_SECURITY_FINDINGS_TOTAL: Final[str] = "homelab_synology_security_findings_total"
M_SECURITY_LAST_SCAN_AGE_SECONDS: Final[str] = "homelab_synology_security_last_scan_age_seconds"
M_SECURITY_LAST_SCAN_TIMESTAMP: Final[str] = "homelab_synology_security_last_scan_timestamp"
M_ACTIVE_CONNECTIONS: Final[str] = "homelab_synology_active_connections"

# --- sysStatus -> numeric severity (alertable). Unknown non-empty string clamps to 3.0.
_STATUS_MAP: Final[dict[str, float]] = {
    "safe": 0.0,
    "warning": 1.0,
    "risk": 2.0,
    "danger": 3.0,
}
# Conservative seed for security_status when no clear status (absent/failed/non-str).
_STATUS_BASELINE: Final[float] = 2.0
# Clamp value for an unknown non-empty sysStatus string (fail-loud).
_STATUS_UNKNOWN: Final[float] = 3.0

# Security Advisor category keys (the items-dict keys).
_CATEGORIES: Final[tuple[str, ...]] = (
    "malware",
    "network",
    "systemCheck",
    "update",
    "userInfo",
)
# Severity keys inside each category's ``fail`` dict. ``outOfDate`` camelCase is verbatim.
_SEVERITIES: Final[tuple[str, ...]] = (
    "danger",
    "warning",
    "risk",
    "outOfDate",
    "info",
)

# --- Live-VERIFIED DSM field keys (captured JSON). logical name -> DSM key.
_SEC_FIELDS: Final[dict[str, str]] = {
    "items": "items",
    "sys_status": "sysStatus",
    "last_scan_time": "lastScanTime",
}
_CONN_FIELDS: Final[dict[str, str]] = {
    "total": "total",
}


# ---------------------------------------------------------------------------
# Multi-fetch wrapper: record-and-continue for INDEPENDENT fetches
# (copied verbatim from STAGE-008-012 updates.py)
# ---------------------------------------------------------------------------


def _fetch(
    ctx: CollectorContext,
    response: SynologyResponse | SynologyError,
    start: float,
    emitted: list[int],
    errors: list[str],
) -> SynologyResponse | None:
    """Wrap fetch_or_result for INDEPENDENT (non-early-returning) fetches.

    On a client error fetch_or_result returns a CollectorResult (errors
    populated); we record those error strings into ``errors`` and return None
    instead of aborting. On success it has already emitted api_took + bumped
    emitted[0]; we return the SynologyResponse.
    """
    r = fetch_or_result(ctx, response, start, emitted)
    if isinstance(r, CollectorResult):
        errors.extend(r.errors)
        return None
    return r


# ---------------------------------------------------------------------------
# Per-tick observation accumulator
# ---------------------------------------------------------------------------


class _Built:
    """Per-tick observation lists, one per cap-routed metric family.

    The two ALWAYS-EMIT scalars are seeded with their baseline default in __init__
    (security_status=2.0 conservative, security_safe=0.0) so a failed/absent security
    fetch still emits the alertable series. A successful parse OVERWRITES them. The
    other five families start empty (emit-on-success / emit-if-present).
    """

    __slots__ = (
        "active_connections_obs",
        "security_findings_obs",
        "security_findings_total_obs",
        "security_last_scan_age_seconds_obs",
        "security_last_scan_timestamp_obs",
        "security_safe_obs",
        "security_status_obs",
    )

    def __init__(self) -> None:
        """Initialise lists; seed the two always-emit scalars with their baseline."""
        self.security_status_obs: list[tuple[dict[str, str], float]] = [({}, _STATUS_BASELINE)]
        self.security_safe_obs: list[tuple[dict[str, str], float]] = [({}, 0.0)]
        self.security_findings_obs: list[tuple[dict[str, str], float]] = []
        self.security_findings_total_obs: list[tuple[dict[str, str], float]] = []
        self.security_last_scan_age_seconds_obs: list[tuple[dict[str, str], float]] = []
        self.security_last_scan_timestamp_obs: list[tuple[dict[str, str], float]] = []
        self.active_connections_obs: list[tuple[dict[str, str], float]] = []


# ---------------------------------------------------------------------------
# Local DSM-shape helper (copied from STAGE-008-010 backup.py)
# ---------------------------------------------------------------------------

# DSM timestamp string format (UTC). lastScanTime may be epoch OR this.
_DSM_TIME_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"


def _parse_dsm_time(v: object) -> float | None:
    """Parse a DSM timestamp to a UTC epoch float.

    Accepts EITHER a numeric epoch (int/float/numeric-str, via as_float) returned
    as-is (the live ``lastScanTime`` is a bare epoch string), OR a string
    ``"%Y-%m-%d %H:%M:%S"`` parsed as UTC. Anything else / unparseable -> None.
    """
    epoch = as_float(v)
    if epoch is not None:
        return epoch
    if isinstance(v, str):
        try:
            parsed = datetime.strptime(v.strip(), _DSM_TIME_FORMAT)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC).timestamp()
    return None


# ---------------------------------------------------------------------------
# Parse passes
# ---------------------------------------------------------------------------


def _parse_security(built: _Built, sec_payload: dict[str, object], now: float) -> None:
    """Parse the Security Advisor payload into the security families.

    OVERWRITES the seeded security_status / security_safe scalars from ``sysStatus``.
    Emits the 5x5 findings grid + the 5 per-severity totals ONLY when ``items`` parses
    to a dict. Emits last_scan_timestamp + derived age ONLY when lastScanTime parses.
    """
    sys_status = nested(sec_payload, _SEC_FIELDS["sys_status"])

    # security_status (overwrite the seeded 2.0 baseline) + security_safe.
    if isinstance(sys_status, str):
        norm = sys_status.lower()
        mapped = _STATUS_MAP.get(norm)
        status_val = mapped if mapped is not None else _STATUS_UNKNOWN
        built.security_status_obs = [({}, status_val)]
        safe_val = 1.0 if norm == "safe" else 0.0
        built.security_safe_obs = [({}, safe_val)]
    # If sys_status is NOT a str, leave the seeded 2.0 / 0.0 baselines untouched.

    # findings grid + per-severity totals (emit only when items is a dict).
    items_dict = as_dict(nested(sec_payload, _SEC_FIELDS["items"]))
    if items_dict is not None:
        totals: dict[str, float] = {sev: 0.0 for sev in _SEVERITIES}
        for cat in _CATEGORIES:
            for sev in _SEVERITIES:
                raw = as_float(nested(items_dict, cat, "fail", sev))
                count = raw if raw is not None else 0.0
                built.security_findings_obs.append(({"category": cat, "severity": sev}, count))
                totals[sev] += count
        for sev in _SEVERITIES:
            built.security_findings_total_obs.append(({"severity": sev}, totals[sev]))

    # last scan timestamp + derived age (emit-if-present).
    scan_time = _parse_dsm_time(nested(sec_payload, _SEC_FIELDS["last_scan_time"]))
    if scan_time is not None:
        built.security_last_scan_timestamp_obs.append(({}, scan_time))
        built.security_last_scan_age_seconds_obs.append(({}, max(0.0, now - scan_time)))


def _parse_connection(built: _Built, conn_payload: dict[str, object]) -> None:
    """Parse the active-connection count (emit-on-success, NO seeded baseline).

    Emits active_connections ONLY when top-level ``total`` is numeric. Absent /
    non-numeric -> emit nothing (the deliberate seed-0 break documented in the module
    docstring). Does NOT iterate the connection ``items`` list.
    """
    total = as_float(nested(conn_payload, _CONN_FIELDS["total"]))
    if total is not None:
        built.active_connections_obs.append(({}, total))


def _emit(
    ctx: CollectorContext, built: _Built, events: list[CollectorEvent], emitted: list[int]
) -> None:
    """Cap-route every family through one CappedEmitter."""
    emitter = capped_emitter(ctx, events)

    def family(name: str, obs: list[tuple[dict[str, str], float]]) -> None:
        emitted[0] += emitter.emit_family(name, cap_for_synology(name), obs) + 1

    family(M_SECURITY_STATUS, built.security_status_obs)
    family(M_SECURITY_SAFE, built.security_safe_obs)
    family(M_SECURITY_FINDINGS, built.security_findings_obs)
    family(M_SECURITY_FINDINGS_TOTAL, built.security_findings_total_obs)
    family(M_SECURITY_LAST_SCAN_AGE_SECONDS, built.security_last_scan_age_seconds_obs)
    family(M_SECURITY_LAST_SCAN_TIMESTAMP, built.security_last_scan_timestamp_obs)
    family(M_ACTIVE_CONNECTIONS, built.active_connections_obs)


class SynologySecurityCollector(BaseCollector):
    """Emit DSM Security Advisor posture + active-connection count from 2 CO-EQUAL DSM APIs.

    Polls once per 1-hour tick in the ``synology`` concurrency group. Neither fetch is
    primary: a single fetch failing records its error but keeps ok=True with the other
    API's families still emitted; ok=False ONLY when BOTH fetches fail. An unconfigured
    client is ok=False. The security_status (seeded 2.0) and security_safe (seeded 0.0)
    scalars ALWAYS emit (the alertable contract); active_connections is emit-on-success
    (the deliberate seed-0 break).
    """

    name: ClassVar[str] = "synology_security"
    interval: ClassVar[timedelta] = timedelta(seconds=3600)
    timeout: ClassVar[timedelta] = timedelta(seconds=30)
    concurrency_group: ClassVar[str] = "synology"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Fetch security scan + connection list co-equally, parse, emit cap-routed families."""
        start = time.monotonic()
        if ctx.synology is None:
            return client_unconfigured_result(start)

        emitted: list[int] = [0]
        errors: list[str] = []
        events: list[CollectorEvent] = []
        built = _Built()
        now = datetime.now(UTC).timestamp()

        # CO-EQUAL fetch 1: Security Advisor scan status.
        sec_resp = _fetch(ctx, await ctx.synology.security_scan_status(), start, emitted, errors)
        if sec_resp is not None:
            sec_payload = as_dict(sec_resp.payload)
            if sec_payload is not None:
                _parse_security(built, sec_payload, now)

        # CO-EQUAL fetch 2: active connection list (COUNTED ONLY).
        conn_resp = _fetch(
            ctx, await ctx.synology.current_connection_list(), start, emitted, errors
        )
        if conn_resp is not None:
            conn_payload = as_dict(conn_resp.payload)
            if conn_payload is not None:
                _parse_connection(built, conn_payload)

        # ALWAYS emit (even on a both-failed run: empty families emit drop gauge only;
        # the seeded security_status/security_safe scalars still emit their single series).
        _emit(ctx, built, events, emitted)

        # CO-EQUAL: ok=False ONLY when BOTH fetches failed.
        ok = sec_resp is not None or conn_resp is not None
        return CollectorResult(
            ok=ok,
            metrics_emitted=emitted[0],
            errors=errors,
            events=events,
            duration_seconds=time.monotonic() - start,
        )
