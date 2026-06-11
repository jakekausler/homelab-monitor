"""ha_update collector — per-entity software-update availability from HA states (STAGE-005-008).

Polls Home Assistant ``GET /api/states`` once per interval, filters to entities
in the ``update`` domain, and emits a single cardinality-capped gauge family:

- ``homelab_ha_update_available{entity_id, title}`` — 1.0 when state is ``on``
  (update available), 0.0 when state is ``off`` (up-to-date). Entities whose
  state is anything else (``unavailable`` / ``unknown`` / empty / unexpected)
  are SKIPPED (NOT emitted as 0), so a stale 0 never masquerades as "up-to-date".

The ``title`` label carries the human-readable package name from
``state.attributes["title"]`` (defaulting to ``""`` if missing or non-str).
Version strings (``installed_version``, ``latest_version``) are intentionally
NOT emitted — they belong to a versions panel (STAGE-021), not here.
Design decision: D-UPDATE-VERSION-CARDINALITY.

The family cap is 150 (set in ``_DEFAULT_CARDINALITY_FAMILIES``; ~106 real update
entities observed on this homelab + headroom).

OK SEMANTICS: an unreachable HA (``ctx.ha is None`` or an ``HaError`` from
``get_states``) is a FAILED run (``ok=False``, ``metrics_emitted=0``). A
reachable HA with zero matching entities is a SUCCESS that emits only the
always-written drop gauge (value 0.0).
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
)

# Metric family name (referenced by both the cap lookup and the emit).
M_UPDATE_AVAILABLE: Final[str] = "homelab_ha_update_available"

# HA domain and state values for software update entities.
_UPDATE_DOMAIN: Final[str] = "update"
_STATE_ON: Final[str] = "on"
_STATE_OFF: Final[str] = "off"


class HaUpdateCollector(BaseCollector):
    """Emit per-entity update-available gauge (1/0) for HA update.* entities."""

    name: ClassVar[str] = "ha_update"
    interval: ClassVar[timedelta] = timedelta(seconds=300)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "homeassistant"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll HA states, filter to update.* entities, emit a capped gauge family."""
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
        update_cap = caps.cap_for(M_UPDATE_AVAILABLE)

        observations: list[tuple[dict[str, str], float]] = []
        for state in result:
            if extract_domain(state.entity_id) != _UPDATE_DOMAIN:
                continue
            if state.state == _STATE_ON:
                value = 1.0
            elif state.state == _STATE_OFF:
                value = 0.0
            else:
                continue  # unavailable / unknown / anything else — skip
            title_obj = state.attributes.get("title")
            title = title_obj if isinstance(title_obj, str) else ""
            labels = {"entity_id": state.entity_id, "title": title}
            observations.append((labels, value))

        events: list[CollectorEvent] = []
        emitter = CappedEmitter(writer=ctx.vm, events=events)
        survivors = emitter.emit_family(M_UPDATE_AVAILABLE, update_cap, observations)

        # CappedEmitter writes one homelab_metric_family_dropped_series gauge per call.
        metrics_emitted = survivors + 1

        return CollectorResult(
            ok=True,
            metrics_emitted=metrics_emitted,
            errors=[],
            events=events,
            duration_seconds=time.monotonic() - start,
        )
