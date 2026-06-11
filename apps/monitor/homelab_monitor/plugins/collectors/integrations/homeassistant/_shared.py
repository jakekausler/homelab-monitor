"""Shared helpers for Home Assistant collectors (STAGE-005-007).

Extracted so the entity-availability (006) and battery-level (007) collectors
share the HA-states fetch seam, the entity-domain split, and numeric-state
parsing. Each collector still constructs its OWN ``CollectorResult`` from the
return of :func:`get_states_or_error`, so error-handling behavior stays
collector-local and byte-identical to the pre-extraction code.
"""

from __future__ import annotations

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
    """Parse a numeric entity state, or ``None`` when it is not a real number.

    Returns ``None`` for the case-sensitive sentinels ``unavailable`` /
    ``unknown`` / empty, and for any string ``float()`` cannot parse. Callers
    skip the entity (do NOT emit 0) when this returns ``None``.
    """
    if state in _NON_NUMERIC_STATES:
        return None
    try:
        return float(state)
    except ValueError:
        return None
