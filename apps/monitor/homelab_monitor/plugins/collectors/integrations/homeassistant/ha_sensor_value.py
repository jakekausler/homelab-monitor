"""ha_sensor_value collector — raw temp/humidity sensor values (STAGE-005-016).

Polls Home Assistant ``GET /api/states`` once per interval, filters to entities
whose ``device_class`` is in a configurable allow-set (``temperature`` /
``humidity`` by default — scoped by device_class, NOT by domain, so freezer /
fridge / indoor temps surface regardless of their domain), and emits a single
cardinality-capped gauge family:

- ``homelab_ha_sensor_value{entity_id, device_class}`` — the entity's numeric
  state as a raw float (via ``parse_float_state`` — rejects non-finite, skips
  ``unavailable`` / ``unknown`` / empty / unparseable). NO ``unit_of_measurement``
  label (labels are EXACTLY ``entity_id`` + ``device_class``).

The preset user-rules seeded in migration 0042 (e.g. PresetFreezerTooWarm) alert on
this metric once an operator edits the placeholder entity_id and enables them.

The family is capped via :class:`CappedEmitter` (global default cap 500 — no entry
in ``_DEFAULT_CARDINALITY_FAMILIES``).

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
    load_sensor_value_config,
)
from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.metrics.cardinality import CappedEmitter
from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.types import CollectorEvent, CollectorResult
from homelab_monitor.plugins.collectors.integrations.homeassistant._shared import (
    get_states_or_error,
    parse_float_state,
)

# Metric family name (referenced by both the cap lookup and the emit).
M_SENSOR_VALUE: Final[str] = "homelab_ha_sensor_value"


class HaSensorValueCollector(BaseCollector):
    """Emit raw numeric values for temp/humidity-classed HA entities."""

    name: ClassVar[str] = "ha_sensor_value"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "homeassistant"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll HA states, filter to temp/humidity sensors, emit a capped gauge family."""
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

        cfg = load_sensor_value_config()
        caps = load_cardinality_caps_config()
        cap = caps.cap_for(M_SENSOR_VALUE)

        observations: list[tuple[dict[str, str], float]] = []
        for state in result:
            device_class = state.attributes.get("device_class")
            if device_class not in cfg.device_classes:
                continue
            value = parse_float_state(state.state)
            if value is None:
                continue
            labels = {"entity_id": state.entity_id, "device_class": str(device_class)}
            observations.append((labels, value))

        events: list[CollectorEvent] = []
        emitter = CappedEmitter(writer=ctx.vm, events=events)
        survivors = emitter.emit_family(M_SENSOR_VALUE, cap, observations)

        # CappedEmitter writes one homelab_metric_family_dropped_series gauge per call.
        metrics_emitted = survivors + 1

        return CollectorResult(
            ok=True,
            metrics_emitted=metrics_emitted,
            errors=[],
            events=events,
            duration_seconds=time.monotonic() - start,
        )
