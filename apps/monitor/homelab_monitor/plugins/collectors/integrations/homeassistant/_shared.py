"""Shared helpers for Home Assistant collectors (STAGE-005-007).

Extracted so the entity-availability (006) and battery-level (007) collectors
share the HA-states fetch seam, the entity-domain split, and numeric-state
parsing. Each collector still constructs its OWN ``CollectorResult`` from the
return of :func:`get_states_or_error`, so error-handling behavior stays
collector-local and byte-identical to the pre-extraction code.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from homelab_monitor.kernel.ha.errors import HaError

if TYPE_CHECKING:
    from homelab_monitor.kernel.ha.client import HaState
    from homelab_monitor.kernel.plugins.context import CollectorContext

# States that mean "no real numeric value" (CASE-SENSITIVE — do NOT lower()).
_NON_NUMERIC_STATES: Final[frozenset[str]] = frozenset({"unavailable", "unknown", ""})


def extract_domain(entity_id: str) -> str:
    """Return the HA domain (text before the first ``.``) of ``entity_id``."""
    return entity_id.partition(".")[0]


async def get_states_or_error(ctx: CollectorContext) -> list[HaState] | HaError:
    """Fetch HA states, mapping a missing client to a typed ``HaError``.

    Returns the entity-state list on success, or an :class:`HaError` when the
    HA client is not configured (``ctx.ha is None``) or when ``get_states``
    itself returns one. The caller builds its own ``CollectorResult`` from the
    result so each collector keeps its existing error semantics.

    The not-configured case maps to ``HaError(reason="unreachable",
    message="ha client not configured")`` so the caller's
    ``errors=[result.message]`` yields the exact string ``"ha client not
    configured"`` (preserving 006's behavior byte-for-byte).
    """
    if ctx.ha is None:
        return HaError(reason="unreachable", message="ha client not configured")
    return await ctx.ha.get_states()


def parse_float_state(state: str) -> float | None:
    """Parse a finite numeric entity state, or ``None`` when it is not one.

    Returns ``None`` for the case-sensitive sentinels ``unavailable`` /
    ``unknown`` / empty, for any string ``float()`` cannot parse, AND for
    non-finite values (``nan`` / ``inf`` / ``-inf``) which ``float()`` accepts
    but which would poison downstream statistics. Callers skip the entity (do
    NOT emit 0) when this returns ``None``.
    """
    if state in _NON_NUMERIC_STATES:
        return None
    try:
        value = float(state)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_iso_or_none(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime, or ``None``.

    Accepts an arbitrary attribute value (HA ``attributes`` values are typed
    ``object``). Returns ``None`` — never raises — when:

    - ``value`` is not a non-empty ``str`` (covers ``None``, missing, ``""``,
      and non-string types), or
    - ``datetime.fromisoformat`` cannot parse it.

    A successfully parsed naive datetime is assumed to be UTC (HA reports UTC).
    This is the pure parse seam: ISO string -> aware datetime | None. It does
    NO seconds math and NO parse-error counting — those stay collector-local.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts
