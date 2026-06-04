"""Severity-stacked log-density histogram (STAGE-004-019, Option A).

GET /api/logs/histogram makes ONE VictoriaLogs ``/select/logsql/hits`` call with
``field=severity`` (exact per-bucket per-severity counts in a single request),
then re-bins VL's epoch/step-aligned timestamps onto START-aligned buckets in
Python and coarse-maps each raw severity token to error/warn/info.

Mirrors ``kernel/logs/fields.py``'s FieldsCache + fetch_* idioms.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from homelab_monitor.kernel.api.schemas import HistogramBucket, LogsHistogramResponse
from homelab_monitor.kernel.logs.models import normalize_severity
from homelab_monitor.kernel.logs.victorialogs_client import VictoriaLogsClient

_CACHE_TTL_SECONDS = 30.0

# Coarse severity buckets surfaced on the stacked chart. ALWAYS all three keys,
# in this fixed render order, so the chart has no gaps.
_COARSE_KEYS = ("error", "warn", "info")

# canonical (from normalize_severity) -> coarse stack key. Anything not listed
# here (info, notice, debug) AND None/unknown fall through to "info".
_COARSE_BY_CANONICAL: dict[str, str] = {
    "error": "error",
    "critical": "error",
    "alert": "error",
    "emergency": "error",
    "warn": "warn",
}


def coarse_bucket(raw_severity: str | None) -> str:
    """Map a RAW severity token to a coarse stack key (error|warn|info).

    Single source of truth: runs ``raw_severity`` through the existing
    ``normalize_severity`` (syslog-numeric / alias / canonical handling), then
    a coarse map. error/critical/alert/emergency -> "error"; warn -> "warn";
    info/notice/debug AND None AND any unknown -> "info". PURE, unit-testable.
    """
    canonical = normalize_severity(raw_severity)
    if canonical is None:
        return "info"
    return _COARSE_BY_CANONICAL.get(canonical, "info")


def parse_iso_to_ms(iso: str) -> int:
    """Parse an ISO-8601 timestamp to integer epoch milliseconds (UTC).

    Naive timestamps are treated as UTC. Used for both the request window
    bounds and VL's per-bucket timestamps. Raises ValueError on unparseable
    input (caller controls window bounds; VL timestamps are well-formed).
    """
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    """Inverse of parse_iso_to_ms: epoch ms -> ISO-8601 UTC string (…+00:00)."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def compute_step_ms(start_ms: int, end_ms: int, buckets: int) -> int:
    """Bucket width in ms: ceil(span / buckets), floored at 1ms.

    ``span = end_ms - start_ms`` (end INCLUSIVE in v0.30.0, but the +1 vs +0 on
    span is immaterial at ms granularity for any real range; we use the raw
    difference). A non-positive span (defensive — window validation forbids it)
    yields step 1.
    """
    span = end_ms - start_ms
    if span <= 0:
        return 1
    return max(1, math.ceil(span / buckets))


def step_ms_to_duration(step_ms: int) -> str:
    """Format step_ms as a VL duration string. RISK #1: ms-suffix, ms-granular.

    VL v0.30.0 accepts the ``ms`` suffix at ms granularity. Validated against the
    real rig at Refinement (integration test). If ever rejected, switch to
    ``f"{max(1, step_ms // 1000)}s"``.
    """
    return f"{step_ms}ms"


def bucket_count(start_ms: int, end_ms: int, step_ms: int) -> int:
    """Number of START-aligned buckets needed to cover [start, end] inclusive.

    Bucket i covers [start + i*step, start + (i+1)*step). With INCLUSIVE end, a
    line at exactly end_ms lands in the last bucket via assign_bucket's clamp.
    n = floor((end - start) / step) + 1, floored at 1.
    """
    span = end_ms - start_ms
    if span <= 0 or step_ms <= 0:
        return 1
    return (span // step_ms) + 1


def assign_bucket(ts_ms: int, start_ms: int, step_ms: int, n: int) -> int:
    """Re-bin a VL timestamp onto a START-aligned bucket index, clamped [0, n-1].

    VL aligns bucket-start timestamps to the step/epoch grid, NOT to ``start``,
    so a returned timestamp can precede ``start`` (negative offset -> clamp 0)
    or, with INCLUSIVE end, equal ``end`` (offset == n -> clamp n-1).
    """
    if step_ms <= 0:
        return 0
    idx = (ts_ms - start_ms) // step_ms
    if idx < 0:
        return 0
    if idx >= n:
        return n - 1
    return idx


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    value: LogsHistogramResponse


class HistogramCache:
    """In-process TTL cache for /api/logs/histogram.

    Keyed by (sha256(effective_expr), start, end, buckets) — the composed expr
    can be ~4KB, so it is hashed. Injectable monotonic clock for deterministic
    tests (mirrors kernel.logs.fields.FieldsCache).
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = _CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[tuple[str, str, str, int], _CacheEntry] = {}

    @staticmethod
    def make_key(*, expr: str, start: str, end: str, buckets: int) -> tuple[str, str, str, int]:
        expr_hash = hashlib.sha256(expr.encode("utf-8")).hexdigest()
        return (expr_hash, start, end, buckets)

    def get(self, key: tuple[str, str, str, int]) -> LogsHistogramResponse | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() >= entry.expires_at:
            del self._entries[key]
            return None
        return entry.value

    def put(self, key: tuple[str, str, str, int], value: LogsHistogramResponse) -> None:
        self._entries[key] = _CacheEntry(expires_at=self._clock() + self._ttl, value=value)


async def fetch_histogram(
    *,
    client: VictoriaLogsClient,
    expr: str,
    start: str,
    end: str,
    buckets: int,
) -> LogsHistogramResponse:
    """Run the single /hits call and shape a LogsHistogramResponse.

    Emits EXACTLY ``n`` buckets (n = bucket_count(...), may be buckets + 1 due to
    VL's inclusive end), time-ascending, each with all three coarse keys present
    (zeros included). Empty hits -> n zero-filled buckets. Raises
    VictoriaLogsClientError on transport / non-200 (caller maps to 502).
    """
    start_ms = parse_iso_to_ms(start)
    end_ms = parse_iso_to_ms(end)
    step_ms = compute_step_ms(start_ms, end_ms, buckets)
    n = bucket_count(start_ms, end_ms, step_ms)
    step_str = step_ms_to_duration(step_ms)

    # Single VL call, grouped by severity.
    series = await client.hits(expr=expr, start=start, end=end, step=step_str, field="severity")

    # n buckets, each {"error":0,"warn":0,"info":0}.
    tallies: list[dict[str, int]] = [{k: 0 for k in _COARSE_KEYS} for _ in range(n)]
    for s in series:
        coarse = coarse_bucket(s.field_value)
        for ts_iso, count in zip(s.timestamps, s.counts, strict=False):
            ts_ms = parse_iso_to_ms(ts_iso)
            idx = assign_bucket(ts_ms, start_ms, step_ms, n)
            tallies[idx][coarse] += count

    out_buckets: list[HistogramBucket] = []
    for i in range(n):
        counts = tallies[i]
        out_buckets.append(
            HistogramBucket(
                start_ts=ms_to_iso(start_ms + i * step_ms),
                counts_by_severity=dict(counts),
                total=sum(counts.values()),
            )
        )

    return LogsHistogramResponse(buckets=out_buckets, bucket_duration_ms=step_ms)


__all__ = [
    "HistogramCache",
    "assign_bucket",
    "bucket_count",
    "coarse_bucket",
    "compute_step_ms",
    "fetch_histogram",
    "ms_to_iso",
    "parse_iso_to_ms",
    "step_ms_to_duration",
]
