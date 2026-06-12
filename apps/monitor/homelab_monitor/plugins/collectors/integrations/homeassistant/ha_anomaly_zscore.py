"""ha_anomaly_zscore collector — per-entity rolling z-score from HA states (STAGE-005-013).

Polls Home Assistant ``GET /api/states`` once per interval, filters to eligible numeric
sensors (``state_class == "measurement"`` AND ``device_class`` in a configurable allow-set,
plus per-entity force-include / force-exclude overrides), and maintains an IN-MEMORY rolling
window of the last N parseable float values PER entity (``collections.deque(maxlen=N)``).
Each tick, for every eligible entity whose window holds ``>= min_samples`` values AND whose
POPULATION std ``>= epsilon``, it emits one capped gauge:

- ``homelab_ha_entity_value_zscore{entity_id}`` — ``(current - rolling_mean) / rolling_pstdev``.
  ``entity_id`` is the ONLY label. The value appended THIS tick IS part of the window used
  for mean/std (the standard rolling-statistic approach: the current observation is included
  in the baseline it is scored against).

BASELINE = Option A (in-memory rolling window seeded only from live per-tick get_states
snapshots). There is NO /api/history call and NO new get_history() REST method. The window
is just floats — no timestamps — so this collector has no UTC/timestamp dependency (the
card's "assert UTC" is N/A for this design).
# Option B (history-seed via /api/history at startup) is the deliberate future upgrade if
# cold-start warmup (min_samples ticks ≈ 1h before the first z-score) proves painful.

ZERO-VARIANCE SKIP: a window whose pstdev is below ``epsilon`` (a flat sensor) emits NO
series for that entity — a z-score over (near-)zero variance is a divide-by-zero blow-up,
not a signal. Detecting a STUCK/flat sensor is the staleness rule's job (entity-availability
/ last-changed), NOT this collector's; this collector only scores VARIATION.

OK SEMANTICS: like ``ha_battery``, an unreachable HA (``ctx.ha is None`` or an ``HaError``
from ``get_states``) is a FAILED run (``ok=False``, ``metrics_emitted=0``) and does NOT
mutate any window. A reachable HA with zero eligible entities (or all still in warmup) is a
SUCCESS that emits only the always-written drop gauge (value 0.0).
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from datetime import timedelta
from typing import TYPE_CHECKING, ClassVar, Final

from homelab_monitor.kernel.config import (
    AnomalyZscoreConfig,
    load_anomaly_zscore_config,
    load_cardinality_caps_config,
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

if TYPE_CHECKING:
    from homelab_monitor.kernel.ha.client import HaState

# Metric family name (referenced by both the cap lookup and the emit).
M_ZSCORE: Final[str] = "homelab_ha_entity_value_zscore"

_STATE_CLASS_MEASUREMENT: Final[str] = "measurement"


def _is_eligible(state: HaState, cfg: AnomalyZscoreConfig) -> bool:
    """Return True when ``state`` should contribute a value to its rolling window.

    Precedence (excluded wins over everything; extra-include wins over the heuristic):
      1. entity_id in ``excluded_entity_ids``      -> NOT eligible (hard exclude).
      2. entity_id in ``extra_entity_ids``         -> eligible (force include).
      3. state_class == "measurement" AND device_class in ``device_classes`` -> eligible.
      4. otherwise                                 -> NOT eligible.

    A parseable-float check is NOT done here — the caller does it after eligibility, so a
    transiently-unavailable eligible sensor is skipped for the tick without leaving the
    eligible set (it simply contributes no value that tick).
    """
    if state.entity_id in cfg.excluded_entity_ids:
        return False
    if state.entity_id in cfg.extra_entity_ids:
        return True
    if state.attributes.get("state_class") != _STATE_CLASS_MEASUREMENT:
        return False
    return state.attributes.get("device_class") in cfg.device_classes


class HaAnomalyZscoreCollector(BaseCollector):
    """Emit per-entity rolling z-score for eligible numeric HA sensors (STATEFUL)."""

    name: ClassVar[str] = "ha_anomaly_zscore"
    interval: ClassVar[timedelta] = timedelta(minutes=5)
    timeout: ClassVar[timedelta] = timedelta(seconds=15)
    concurrency_group: ClassVar[str] = "homeassistant"

    def __init__(self) -> None:
        """Initialize the cross-tick per-entity rolling-window store.

        ``_windows`` maps entity_id -> a bounded deque of recent float values. The
        deque's ``maxlen`` is (re)applied each tick from config; see ``run`` for the
        window-resize handling when an operator changes ``window_samples`` live.
        """
        self._windows: dict[str, deque[float]] = {}

    def _window_for(self, entity_id: str, maxlen: int) -> deque[float]:
        """Return the entity's window, creating (or resizing) it to ``maxlen``.

        A live ``window_samples`` change rebuilds the deque preserving the most recent
        values (deque(existing, maxlen=new) keeps the last ``new`` entries). When the
        window doesn't exist yet, a fresh empty deque is created.
        """
        existing = self._windows.get(entity_id)
        if existing is None:
            window: deque[float] = deque(maxlen=maxlen)
            self._windows[entity_id] = window
            return window
        if existing.maxlen != maxlen:
            window = deque(existing, maxlen=maxlen)
            self._windows[entity_id] = window
            return window
        return existing

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        """Poll HA states, update rolling windows, emit a capped z-score gauge family."""
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

        cfg = load_anomaly_zscore_config()
        caps = load_cardinality_caps_config()
        zscore_cap = caps.cap_for(M_ZSCORE)

        observations: list[tuple[dict[str, str], float]] = []
        for state in result:
            if not _is_eligible(state, cfg):
                continue
            value = parse_float_state(state.state)
            if value is None:
                continue
            window = self._window_for(state.entity_id, cfg.window_samples)
            window.append(value)
            if len(window) < cfg.min_samples:
                continue
            mean = statistics.fmean(window)
            std = statistics.pstdev(window)
            if std < cfg.zero_variance_epsilon:
                continue
            zscore = (value - mean) / std
            observations.append(({"entity_id": state.entity_id}, zscore))

        events: list[CollectorEvent] = []
        emitter = CappedEmitter(writer=ctx.vm, events=events)
        survivors = emitter.emit_family(M_ZSCORE, zscore_cap, observations)

        # CappedEmitter writes one homelab_metric_family_dropped_series gauge per call.
        metrics_emitted = survivors + 1

        return CollectorResult(
            ok=True,
            metrics_emitted=metrics_emitted,
            errors=[],
            events=events,
            duration_seconds=time.monotonic() - start,
        )
