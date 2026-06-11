"""ha_battery collector — per-entity battery level from HA states (STAGE-005-007).

Polls Home Assistant ``GET /api/states`` once per interval, filters to entities
whose ``device_class`` is ``battery`` AND whose ``unit_of_measurement`` is ``%``,
and emits a single cardinality-capped gauge family:

- ``homelab_ha_battery_level{entity_id, domain}`` — the entity's numeric state as
  a percentage (0..100). Entities whose state is non-numeric
  (``unavailable`` / ``unknown`` / empty / unparseable) are SKIPPED (NOT emitted
  as 0), so a stale 0 never masquerades as a flat battery.

The family is capped via :class:`CappedEmitter`, which also auto-emits
``homelab_metric_family_dropped_series{family}`` and a single warning
``SuggestionEvent`` when the family exceeds its cardinality budget. The battery
family has no entry in ``_DEFAULT_CARDINALITY_FAMILIES`` and so uses the global
default cap (500).

OK SEMANTICS: like ``ha_entity_available``, an unreachable HA (``ctx.ha is None``
or an ``HaError`` from ``get_states``) is a FAILED run (``ok=False``,
``metrics_emitted=0``). A reachable HA with zero matching entities is a SUCCESS
that emits only the always-written drop gauge (value 0.0).
"""

from __future__ import annotations

import time
from datetime import timedelta
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
    parse_float_state,
)

# Metric family name (referenced by both the cap lookup and the emit).
M_BATTERY_LEVEL: Final[str] = "homelab_ha_battery_level"

# HA attribute markers identifying a battery-percentage sensor.
_DEVICE_CLASS_BATTERY: Final[str] = "battery"
_UNIT_PERCENT: Final[str] = "%"


class HaBatteryCollector(BaseCollector):
    """Emit per-entity battery level (%) for battery-classed HA entities."""

    name: ClassVar[str] = "ha_battery"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "homeassistant"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll HA states, filter to battery-% entities, emit a capped gauge family."""
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

        caps = load_cardinality_caps_config()
        battery_cap = caps.cap_for(M_BATTERY_LEVEL)

        observations: list[tuple[dict[str, str], float]] = []
        for state in result:
            if state.attributes.get("device_class") != _DEVICE_CLASS_BATTERY:
                continue
            if state.attributes.get("unit_of_measurement") != _UNIT_PERCENT:
                continue
            value = parse_float_state(state.state)
            if value is None:
                continue
            labels = {"entity_id": state.entity_id, "domain": extract_domain(state.entity_id)}
            observations.append((labels, value))

        events: list[CollectorEvent] = []
        emitter = CappedEmitter(writer=ctx.vm, events=events)
        survivors = emitter.emit_family(M_BATTERY_LEVEL, battery_cap, observations)

        # CappedEmitter writes one homelab_metric_family_dropped_series gauge per call.
        metrics_emitted = survivors + 1

        return CollectorResult(
            ok=True,
            metrics_emitted=metrics_emitted,
            errors=[],
            events=events,
            duration_seconds=time.monotonic() - start,
        )
