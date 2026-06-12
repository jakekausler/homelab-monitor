"""ha_safety_sensors collector — life-safety binary-sensor states (STAGE-005-016).

Polls Home Assistant ``GET /api/states`` once per interval, filters to
``binary_sensor`` entities whose ``device_class`` is in a configurable safety
allow-set (smoke / gas / carbon_monoxide / moisture / door / window / opening by
default), and emits a single cardinality-capped gauge family:

- ``homelab_ha_binary_sensor_on{entity_id, domain, device_class}`` — ``1.0`` when
  the entity state is ``"on"``, ``0.0`` when ``"off"``. Any other state
  (``unavailable`` / ``unknown`` / empty / anything else) is SKIPPED (NOT emitted),
  so a stale value never masquerades as a triggered safety sensor.

The family is capped via :class:`CappedEmitter` (global default cap 500 — no entry
in ``_DEFAULT_CARDINALITY_FAMILIES``; ~<=100 safety series expected). The vmalert
rules in ``deploy/vmalert/metrics/home-assistant-safety.yaml`` alert on this metric.

OK SEMANTICS: like ``ha_battery``, an unreachable HA (``ctx.ha is None`` or an
``HaError`` from ``get_states``) is a FAILED run (``ok=False``,
``metrics_emitted=0``). A reachable HA with zero matching entities is a SUCCESS
that emits only the always-written drop gauge (value 0.0).
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import ClassVar, Final

from homelab_monitor.kernel.config import (
    load_cardinality_caps_config,
    load_safety_sensors_config,
)
from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.metrics.cardinality import CappedEmitter
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorEvent, CollectorResult
from homelab_monitor.plugins.collectors.integrations.homeassistant._shared import (
    extract_domain,
    get_states_or_error,
)

# Metric family name (referenced by both the cap lookup and the emit).
M_BINARY_SENSOR_ON: Final[str] = "homelab_ha_binary_sensor_on"

_DOMAIN_BINARY_SENSOR: Final[str] = "binary_sensor"
_STATE_ON: Final[str] = "on"
_STATE_OFF: Final[str] = "off"


class HaSafetySensorsCollector(BaseCollector):
    """Emit on/off state for safety-classed binary_sensor HA entities."""

    name: ClassVar[str] = "ha_safety_sensors"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "homeassistant"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll HA states, filter to safety binary_sensors, emit a capped gauge family."""
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

        cfg = load_safety_sensors_config()
        caps = load_cardinality_caps_config()
        cap = caps.cap_for(M_BINARY_SENSOR_ON)

        observations: list[tuple[dict[str, str], float]] = []
        for state in result:
            if extract_domain(state.entity_id) != _DOMAIN_BINARY_SENSOR:
                continue
            device_class = state.attributes.get("device_class")
            if device_class not in cfg.device_classes:
                continue
            if state.state == _STATE_ON:
                value = 1.0
            elif state.state == _STATE_OFF:
                value = 0.0
            else:
                continue
            labels = {
                "entity_id": state.entity_id,
                "domain": _DOMAIN_BINARY_SENSOR,
                "device_class": str(device_class),
            }
            observations.append((labels, value))

        events: list[CollectorEvent] = []
        emitter = CappedEmitter(writer=ctx.vm, events=events)
        survivors = emitter.emit_family(M_BINARY_SENSOR_ON, cap, observations)

        # CappedEmitter writes one homelab_metric_family_dropped_series gauge per call.
        metrics_emitted = survivors + 1

        return CollectorResult(
            ok=True,
            metrics_emitted=metrics_emitted,
            errors=[],
            events=events,
            duration_seconds=time.monotonic() - start,
        )
