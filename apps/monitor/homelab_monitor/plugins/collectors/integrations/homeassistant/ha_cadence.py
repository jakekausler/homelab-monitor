"""ha_cadence collector — automation/script run-cadence from HA states (STAGE-005-009).

Polls Home Assistant ``GET /api/states`` once per interval, filters to entities
in the ``automation`` and ``script`` domains, and emits raw cadence metrics:

- ``homelab_ha_automation_last_triggered_seconds{entity_id}`` — seconds since the
  automation's ``last_triggered`` attribute (now_utc - last_triggered), clamped >= 0.
  Emitted ONLY for ENABLED (state ``on``) automations; disabled (``off``),
  ``unavailable``, ``unknown``, and any other non-``on`` state automations are excluded.
  Their ``automation_enabled`` series still carries them (0.0 for ``off``, skipped for others).
- ``homelab_ha_script_last_triggered_seconds{entity_id}`` — same, for scripts.
- ``homelab_ha_automation_enabled{entity_id}`` — 1.0 when state is ``on``, 0.0 when
  ``off``; any other state (``unavailable`` / ``unknown`` / empty / unexpected) is SKIPPED.
- ``homelab_ha_cadence_last_triggered_parse_errors`` — count of entities whose present
  ``last_triggered`` attribute failed to parse (single series, NO label). Emitted ONLY
  when the count > 0.

Scripts have NO enabled metric — only ``script_last_triggered_seconds``.

NEVER-TRIGGERED IS NOT AN ERROR: an automation/script that has never run has a
missing/``None`` ``last_triggered``. That entity is SKIPPED for the last-triggered
family and is NOT counted as a parse error. A present-but-unparseable
``last_triggered`` IS counted as a parse error and skipped.

This collector emits ONLY raw metrics. There is NO threshold / expected-interval /
max-idle config here — alerting on stale cadence is STAGE-005-015's concern
(design decision D-CADENCE-EXPECTED-CONFIG).

The three per-entity families are cardinality-capped via :class:`CappedEmitter`,
which also auto-emits ``homelab_metric_family_dropped_series{family}`` and a single
warning ``SuggestionEvent`` when a family exceeds its budget. No cadence-specific
family cap is registered, so the global default cap (500) applies — automation/script
counts on this homelab are modest (design decision: no _DEFAULT_CARDINALITY_FAMILIES entry).

OK SEMANTICS: an unreachable HA (``ctx.ha is None`` or an ``HaError`` from
``get_states``) is a FAILED run (``ok=False``, ``metrics_emitted=0``). A reachable HA
with zero matching entities is a SUCCESS that emits only the three always-written drop
gauges (value 0.0).
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
M_AUTOMATION_LAST_TRIGGERED: Final[str] = "homelab_ha_automation_last_triggered_seconds"
M_SCRIPT_LAST_TRIGGERED: Final[str] = "homelab_ha_script_last_triggered_seconds"
M_AUTOMATION_ENABLED: Final[str] = "homelab_ha_automation_enabled"
M_PARSE_ERRORS: Final[str] = "homelab_ha_cadence_last_triggered_parse_errors"

# HA domains this collector observes.
_AUTOMATION_DOMAIN: Final[str] = "automation"
_SCRIPT_DOMAIN: Final[str] = "script"

# Automation enabled-state values (CASE-SENSITIVE — do NOT lower()).
_STATE_ON: Final[str] = "on"
_STATE_OFF: Final[str] = "off"

# Attribute key carrying the last-trigger timestamp.
_LAST_TRIGGERED_ATTR: Final[str] = "last_triggered"


def _triggered_seconds_or_error(
    lt: object,
    now: datetime,
) -> tuple[float | None, bool]:
    """Parse a ``last_triggered`` attribute value into elapsed seconds.

    Returns a ``(seconds, is_parse_error)`` tuple:

    - ``(None, False)`` — ``lt`` is ``None`` (never triggered; skip, NOT an error).
    - ``(None, True)``  — ``lt`` is present but unparseable (count as parse error).
    - ``(seconds, False)`` — valid timestamp; seconds >= 0.0 elapsed since ``now``.
    """
    if lt is None:
        return None, False
    ts = parse_iso_or_none(lt)
    if ts is None:
        return None, True
    return max((now - ts).total_seconds(), 0.0), False


class HaCadenceCollector(BaseCollector):
    """Emit automation/script run-cadence + automation enabled-state from HA states."""

    name: ClassVar[str] = "ha_cadence"
    interval: ClassVar[timedelta] = timedelta(seconds=60)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "homeassistant"

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll HA states, filter to automation/script, emit capped cadence families."""
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

        now = datetime.now(UTC)
        caps = load_cardinality_caps_config()

        automation_triggered_obs: list[tuple[dict[str, str], float]] = []
        script_triggered_obs: list[tuple[dict[str, str], float]] = []
        automation_enabled_obs: list[tuple[dict[str, str], float]] = []
        parse_errors = 0

        for state in result:
            domain = extract_domain(state.entity_id)

            if domain == _AUTOMATION_DOMAIN:
                labels = {"entity_id": state.entity_id}

                # Enabled state: on -> 1.0, off -> 0.0, anything else -> skip.
                if state.state == _STATE_ON:
                    automation_enabled_obs.append((labels, 1.0))
                elif state.state == _STATE_OFF:
                    automation_enabled_obs.append((labels, 0.0))

                # Last-triggered (enabled automations only): distinguish missing
                # (skip, no error) from present-but-unparseable (count error, skip).
                lt = state.attributes.get(_LAST_TRIGGERED_ATTR)
                seconds, err = _triggered_seconds_or_error(lt, now)
                if err:
                    parse_errors += 1
                elif seconds is not None and state.state == _STATE_ON:
                    automation_triggered_obs.append((labels, seconds))

            elif domain == _SCRIPT_DOMAIN:
                labels = {"entity_id": state.entity_id}

                # Scripts: last-triggered only (no enabled metric).
                lt = state.attributes.get(_LAST_TRIGGERED_ATTR)
                seconds, err = _triggered_seconds_or_error(lt, now)
                if err:
                    parse_errors += 1
                elif seconds is not None:
                    script_triggered_obs.append((labels, seconds))

        events: list[CollectorEvent] = []
        emitter = CappedEmitter(writer=ctx.vm, events=events)
        s1 = emitter.emit_family(
            M_AUTOMATION_LAST_TRIGGERED,
            caps.cap_for(M_AUTOMATION_LAST_TRIGGERED),
            automation_triggered_obs,
        )
        s2 = emitter.emit_family(
            M_SCRIPT_LAST_TRIGGERED,
            caps.cap_for(M_SCRIPT_LAST_TRIGGERED),
            script_triggered_obs,
        )
        s3 = emitter.emit_family(
            M_AUTOMATION_ENABLED,
            caps.cap_for(M_AUTOMATION_ENABLED),
            automation_enabled_obs,
        )

        # CappedEmitter writes one homelab_metric_family_dropped_series gauge per call.
        metrics_emitted = s1 + s2 + s3 + 3

        if parse_errors > 0:
            ctx.vm.write_gauge(M_PARSE_ERRORS, float(parse_errors), {})
            metrics_emitted += 1

        return CollectorResult(
            ok=True,
            metrics_emitted=metrics_emitted,
            errors=[],
            events=events,
            duration_seconds=time.monotonic() - start,
        )
