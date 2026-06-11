"""ha_entity_available collector — per-entity availability + staleness from HA states.

Polls Home Assistant ``GET /api/states`` once per interval, filters entities by domain
(allow/deny), and emits two cardinality-capped metric families plus a self-diagnostic
parse-error counter:

- ``homelab_ha_entity_available{entity_id, domain}`` — 1.0 if the entity reports a real
  value, 0.0 if its state is ``unavailable`` / ``unknown`` / empty (CASE-SENSITIVE).
- ``homelab_ha_entity_last_changed_seconds{entity_id, domain}`` — seconds since the entity
  last changed (now_utc - last_changed), clamped >= 0 to absorb clock skew. Emitted for ALL
  filtered entities including unavailable ones.
- ``homelab_ha_entity_last_changed_parse_errors`` — count of entities whose ``last_changed``
  failed to parse (single series, NO per-entity label). Emitted ONLY when the count > 0.

The two per-entity families are capped via :class:`CappedEmitter`, which also auto-emits
``homelab_metric_family_dropped_series{family}`` and a single warning ``SuggestionEvent`` when a
family exceeds its cardinality budget.

OK SEMANTICS: unlike ``ha_up`` (which emits up=0 when HA is down), this collector has nothing to
emit if HA is unreachable, so an unreachable HA (``ctx.ha is None`` or an ``HaError`` from
``get_states``) is a FAILED run (``ok=False``, ``metrics_emitted=0``).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Final

from homelab_monitor.kernel.config import load_cardinality_caps_config
from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.metrics.cardinality import CappedEmitter
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorEvent, CollectorResult
from homelab_monitor.plugins.collectors.integrations.homeassistant._shared import (
    extract_domain,
    get_states_or_error,
    parse_iso_or_none,
)

# Metric family names (referenced by both the cap lookup and the emit).
M_ENTITY_AVAILABLE: Final[str] = "homelab_ha_entity_available"
M_ENTITY_LAST_CHANGED_SECONDS: Final[str] = "homelab_ha_entity_last_changed_seconds"
M_ENTITY_PARSE_ERRORS: Final[str] = "homelab_ha_entity_last_changed_parse_errors"

# States that mean "no real value" (CASE-SENSITIVE — do NOT lower()).
_UNAVAILABLE_STATES: Final[frozenset[str]] = frozenset({"unavailable", "unknown", ""})

# Default domains to observe when ctx.config does not override.
DEFAULT_DOMAIN_ALLOW: Final[frozenset[str]] = frozenset(
    {
        "sensor",
        "binary_sensor",
        "switch",
        "light",
        "climate",
        "lock",
        "cover",
        "fan",
        "device_tracker",
        "media_player",
        "vacuum",
        "water_heater",
        "humidifier",
        "alarm_control_panel",
        "camera",
        "siren",
        "valve",
    }
)


class HaEntityAvailableCollector(BaseCollector):
    """Emit per-entity availability + staleness for allow-listed HA domains."""

    name: ClassVar[str] = "ha_entity_available"
    interval: ClassVar[timedelta] = timedelta(seconds=30)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "homeassistant"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll HA states, filter by domain, emit capped availability + staleness families."""
        start = time.monotonic()

        result = await get_states_or_error(ctx)
        if isinstance(result, HaError):
            return CollectorResult(
                ok=False,
                metrics_emitted=0,
                errors=[result.message],
                events=[],
                duration_seconds=time.monotonic() - start,
            )

        # Resolve domain allow/deny (getattr — these fields are not on the base CollectorConfig;
        # mirrors the host.py getattr-with-default idiom). Coerce to sets for membership checks.
        allow: set[str] = set(getattr(ctx.config, "ha_domains_allow", DEFAULT_DOMAIN_ALLOW))
        deny: set[str] = set(getattr(ctx.config, "ha_domains_deny", []))

        now = datetime.now(UTC)
        caps = load_cardinality_caps_config()
        available_cap = caps.cap_for(M_ENTITY_AVAILABLE)
        last_changed_cap = caps.cap_for(M_ENTITY_LAST_CHANGED_SECONDS)

        available_obs: list[tuple[dict[str, str], float]] = []
        last_changed_obs: list[tuple[dict[str, str], float]] = []
        parse_errors = 0

        for state in result:
            domain = extract_domain(state.entity_id)
            if domain not in allow or domain in deny:
                continue

            labels = {"entity_id": state.entity_id, "domain": domain}

            available = 0.0 if state.state.strip() in _UNAVAILABLE_STATES else 1.0
            available_obs.append((labels, available))

            changed_at = parse_iso_or_none(state.last_changed)
            if changed_at is None:
                parse_errors += 1
                continue

            seconds = (now - changed_at).total_seconds()
            seconds = max(seconds, 0.0)
            last_changed_obs.append((labels, seconds))

        events: list[CollectorEvent] = []
        emitter = CappedEmitter(writer=ctx.vm, events=events)
        survivors_available = emitter.emit_family(M_ENTITY_AVAILABLE, available_cap, available_obs)
        survivors_last_changed = emitter.emit_family(
            M_ENTITY_LAST_CHANGED_SECONDS, last_changed_cap, last_changed_obs
        )

        # CappedEmitter writes one homelab_metric_family_dropped_series gauge per emit_family call.
        metrics_emitted = survivors_available + survivors_last_changed + 2

        if parse_errors > 0:
            ctx.vm.write_gauge(M_ENTITY_PARSE_ERRORS, float(parse_errors), {})
            metrics_emitted += 1

        return CollectorResult(
            ok=True,
            metrics_emitted=metrics_emitted,
            errors=[],
            events=events,
            duration_seconds=time.monotonic() - start,
        )
