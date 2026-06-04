"""Filter-scope-aware field discovery (STAGE-004-018, Option C Hybrid).

GET /api/logs/fields makes TWO fixed VictoriaLogs HTTP calls — regardless of
field count — and merges them:

1. ``client.field_names(expr, start, end)`` → authoritative complete field-name
   list + per-field hit counts. Coverage = field_hits / total (the ``_msg``
   entry's hits == total matching lines).
2. ``client.query(expr, start, end)`` with the existing bounded client (HTTP
   ``limit`` set to sample_n via ``with_limits``) → up to K distinct sample
   values per field + a type hint, mapped ONTO the field_names list.

A rare field present in field_names but absent from the most-recent sample shows
accurate coverage with empty ``sample_values`` + ``type_hint="unknown"`` — the
accepted v1 trade-off (the coverage% self-documents rarity).

Mirrors ``kernel/logs/services.py``'s ServicesCache + fetch_* idioms.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from homelab_monitor.kernel.api.schemas import FieldDescriptor, LogsFieldsResponse
from homelab_monitor.kernel.config import VlQueryLimits, load_vl_query_limits
from homelab_monitor.kernel.logs.victorialogs_client import VictoriaLogsClient

# VictoriaLogs builtins NOT surfaced as discoverable, filterable fields:
#   _stream_id — opaque internal stream identity, not user-queryable as a field.
#   _time      — the line timestamp; surfaced by the time-range control, not a field.
#   _msg       — the message body; it is always-present (coverage denominator) and
#                already filterable via the dedicated message/add-msg-filter path,
#                so it is NOT a "field" to discover. Excluding it also keeps the
#                100%-coverage _msg row out of the field list where it would always
#                sort first and add noise. Mirrors VlLogLine.fields, which excludes
#                exactly these three builtins.
_EXCLUDED_FIELDS = frozenset({"_stream_id", "_time", "_msg"})

# Coverage denominator field. _msg is on every matching line, so its hit count is
# the total matching-line population.
_TOTAL_FIELD = "_msg"

_CACHE_TTL_SECONDS = 30.0

# Distinct sample values surfaced per field.
_DEFAULT_K_VALUES = 5

# Type-hint tokens.
_TYPE_UNKNOWN = "unknown"
_TYPE_NUMERIC = "numeric"
_TYPE_BOOL = "bool"
_TYPE_STRING = "string"
_TYPE_OBJECT = "object"
_TYPE_ARRAY = "array"
_TYPE_MIXED = "mixed"

_BOOL_TOKENS = frozenset({"true", "false"})


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    value: LogsFieldsResponse


class FieldsCache:
    """In-process TTL cache for /api/logs/fields.

    Keyed by (sha256(effective_expr), start, end, sample_n) — the composed expr
    can be ~4KB, so it is hashed. Injectable monotonic clock for deterministic
    tests (mirrors kernel.logs.services.ServicesCache).
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
    def make_key(*, expr: str, start: str, end: str, sample_n: int) -> tuple[str, str, str, int]:
        expr_hash = hashlib.sha256(expr.encode("utf-8")).hexdigest()
        return (expr_hash, start, end, sample_n)

    def get(self, key: tuple[str, str, str, int]) -> LogsFieldsResponse | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() >= entry.expires_at:
            del self._entries[key]
            return None
        return entry.value

    def put(self, key: tuple[str, str, str, int], value: LogsFieldsResponse) -> None:
        self._entries[key] = _CacheEntry(expires_at=self._clock() + self._ttl, value=value)


def infer_type_hint(values: list[str]) -> str:
    """Infer a coarse type from a field's stringified sample values.

    Pure + deterministic (unit-testable in isolation). Rules, in order:
      - [] (no sampled values)          → "unknown"
      - all parse as int OR float       → "numeric"
      - all in {"true","false"} (ci)    → "bool"
      - all start with "{"              → "object"
      - all start with "["              → "array"
      - all are non-empty plain strings → "string"
      - otherwise                       → "mixed"

    Each rule is evaluated against ALL values; the first whose predicate holds
    for every value wins. "numeric" is checked before "bool"/"string" so a field
    of pure numbers is numeric. A value that is the empty string "" is NOT
    numeric/bool/object/array; an all-empty set falls through to "string".
    """
    if not values:
        return _TYPE_UNKNOWN
    if all(_is_numeric(v) for v in values):
        return _TYPE_NUMERIC
    if all(v.strip().lower() in _BOOL_TOKENS for v in values):
        return _TYPE_BOOL
    if all(v.startswith("{") for v in values):
        return _TYPE_OBJECT
    if all(v.startswith("[") for v in values):
        return _TYPE_ARRAY
    return (
        _TYPE_STRING
        if all(not v.startswith(("{", "[")) and not _is_numeric(v) for v in values)
        else _TYPE_MIXED
    )


def _is_numeric(value: str) -> bool:
    """True if `value` parses as a Python int or float (rejects '', 'nan'-words?).

    ``float("nan")`` / ``float("inf")`` parse successfully in Python; treat them
    as numeric (they are numeric tokens). Empty string is NOT numeric.
    """
    if value == "":
        return False
    try:
        float(value)
    except ValueError:
        return False
    return True


async def fetch_fields(  # noqa: PLR0913 -- hybrid discovery requires client + time-window + sampling params
    *,
    client: VictoriaLogsClient,
    expr: str,
    start: str,
    end: str,
    sample_n: int,
    k_values: int = _DEFAULT_K_VALUES,
) -> LogsFieldsResponse:
    """Run the 2-call hybrid discovery and shape a LogsFieldsResponse.

    Raises VictoriaLogsClientError on transport / non-200 from EITHER VL call
    (caller maps to 502).
    """
    # Call 1 — authoritative names + hit counts.
    name_hits = await client.field_names(expr=expr, start=start, end=end)

    total = 0
    for name, hits in name_hits:
        if name == _TOTAL_FIELD:
            total = hits
            break

    # Call 2 — bounded most-recent sample for values + type hints. Reuse the
    # existing query() primitive with a per-request limit of sample_n. The
    # client's max_bytes/timeout are kept; only max_lines is overridden so the
    # sample is exactly the sample_n most-recent lines (truncated=True ⇒ more
    # lines existed than were sampled).
    base_limits = load_vl_query_limits()
    sample_limits = VlQueryLimits(
        max_lines=sample_n,
        max_bytes=base_limits.max_bytes,
        timeout_seconds=base_limits.timeout_seconds,
    )
    sample_client = client.with_limits(sample_limits)
    sample = await sample_client.query(expr=expr, start=start, end=end)

    # Accumulate per-field distinct sample values (first-seen order, capped at K).
    samples: dict[str, list[str]] = {}
    for line in sample.lines:
        for field_name, value in line.fields.items():
            if field_name in _EXCLUDED_FIELDS:  # pragma: no cover
                continue
            seen = samples.setdefault(field_name, [])
            if len(seen) < k_values and value not in seen:
                seen.append(value)

    descriptors: list[FieldDescriptor] = []
    for name, hits in name_hits:
        if name in _EXCLUDED_FIELDS:
            continue
        coverage = (hits / total) if total > 0 else 0.0
        # Clamp defensively: a field's hits should never exceed the _msg total,
        # but VL edge cases / racing windows must not yield coverage > 1.0.
        coverage = min(coverage, 1.0)
        values = samples.get(name, [])
        descriptors.append(
            FieldDescriptor(
                name=name,
                sample_values=values,
                coverage=coverage,
                type_hint=infer_type_hint(values),
            )
        )

    # Sort DESC by coverage, tie-break by name ASC (stable, deterministic).
    descriptors.sort(key=lambda d: (-d.coverage, d.name))

    return LogsFieldsResponse(
        fields=descriptors,
        sampled_lines=len(sample.lines),
        truncated=sample.truncated,
    )


__all__ = ["FieldsCache", "fetch_fields", "infer_type_hint"]
