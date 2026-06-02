"""Distinct `(service, source_type)` discovery + count for the Logs Explorer stream picker.

STAGE-004-012A. Runs a VictoriaLogs `* | stats by (service, source_type) count() as count`
query over a bounded window and returns the distinct (service, source_type) identities
sorted DESC by count, with a top-N truncation flag. Results are cached in-process for
a short TTL keyed on (start, end, limit).

FORWARD-COMPAT: STAGE-004-018's future /api/logs/fields will generalize
distinct-value+count discovery and may absorb/replace this. Do not couple new
callers to this module beyond the stream picker.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from homelab_monitor.kernel.api.schemas import LogsServicesResponse, ServiceCount
from homelab_monitor.kernel.logs.victorialogs_client import (
    VictoriaLogsClient,
)

# LogsQL distinct-values+count. `stats by (service, source_type) count() as count` is the
# documented distinct+count mechanism. NEVER use `sort offset limit` (OOMs).
_SERVICES_STATS_QUERY = "* | stats by (service, source_type) count() as count"

_CACHE_TTL_SECONDS = 30.0


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    value: LogsServicesResponse


class ServicesCache:
    """In-process TTL cache keyed on (start, end, limit).

    The clock is injectable for deterministic tests (mirrors
    kernel.heartbeat.rate_limiter.CronRateLimiter).
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = _CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[tuple[str, str, int], _CacheEntry] = {}

    def get(self, key: tuple[str, str, int]) -> LogsServicesResponse | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() >= entry.expires_at:
            del self._entries[key]
            return None
        return entry.value

    def put(self, key: tuple[str, str, int], value: LogsServicesResponse) -> None:
        self._entries[key] = _CacheEntry(expires_at=self._clock() + self._ttl, value=value)


async def fetch_services(
    *,
    client: VictoriaLogsClient,
    start: str,
    end: str,
    limit: int,
) -> LogsServicesResponse:
    """Run the VL stats query, group by (service, source_type), sort DESC, top-N.

    Returns ONE ServiceCount per distinct (service, source_type) identity — the
    same service name under two source_types yields TWO rows. A VL row missing or
    with an empty source_type is grouped under "unknown" so pre-STAGE-004-012A
    lines (shipped before Vector tagged source_type) still appear.

    Raises VictoriaLogsClientError on transport / non-200 (caller maps to 502).
    """
    result = await client.query(expr=_SERVICES_STATS_QUERY, start=start, end=end)

    counts: list[ServiceCount] = []
    for line in result.lines:
        service = line.fields.get("service")
        raw_count = line.fields.get("count")
        if not service or raw_count is None:
            continue
        try:
            count = int(raw_count)
        except ValueError:
            continue
        # Missing/empty source_type → "unknown" (pre-012A lines, or VL emitting
        # an empty string for an absent grouped field).
        source_type = line.fields.get("source_type") or "unknown"
        counts.append(ServiceCount(service=service, source_type=source_type, count=count))

    # Sort DESC by count; tie-break by (service, source_type) ASC for stable tests.
    counts.sort(key=lambda c: (-c.count, c.service, c.source_type))

    truncated = result.truncated or len(counts) > limit
    if truncated:
        counts = counts[:limit]
    return LogsServicesResponse(services=counts, truncated=truncated)


__all__ = ["ServicesCache", "fetch_services"]
