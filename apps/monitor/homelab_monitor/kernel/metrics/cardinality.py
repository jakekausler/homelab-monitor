"""Per-metric-family cardinality cap (STAGE-005-004).

A *family* is a metric name (e.g. ``homelab_ha_entity_available``). When a
collector can emit an unbounded number of distinct label-sets for one family
(one series per HA entity, per container, per …), an upstream change can blow
up VictoriaMetrics cardinality. This module provides an OPT-IN, per-tick cap a
collector applies inside its own ``run()``:

- :class:`CardinalityCapper` — pure, synchronous. Given a per-tick cap and a
  list of ``(labels, value)`` observations, keeps a deterministic first-N
  subset (stable-sort by canonical label-set key) and reports how many it
  dropped. No I/O, no async, no config dependency.
- :class:`CappedEmitter` — binds a :class:`MetricsWriter` and a collector's
  ``events`` list. ``emit_family`` applies the capper, writes survivors as
  gauges, emits the ``homelab_metric_family_dropped_series`` observability
  gauge, and appends ONE over-budget :class:`SuggestionEvent` when the family
  exceeded its cap.

Determinism: the survivor set is a pure function of the candidate set and the
cap (sorted by ``tuple(sorted(labels.items()))``), independent of the input
order. The same candidates produce the same survivors every tick — no
flapping, no cross-tick state.

This module wires up NO collector. The first consumer is the
entity-availability collector in STAGE-005-006.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from homelab_monitor.kernel.plugins.types import SuggestionEvent

if TYPE_CHECKING:
    from homelab_monitor.kernel.plugins.io import MetricsWriter
    from homelab_monitor.kernel.plugins.types import CollectorEvent

#: Observability gauge: how many series a family dropped on the last tick.
#: Always emitted (0 when under budget) so a recovered family reports 0 rather
#: than letting its series go stale.
M_FAMILY_DROPPED_SERIES: Final[str] = "homelab_metric_family_dropped_series"


@dataclass(frozen=True, slots=True)
class CapResult:
    """Outcome of :meth:`CardinalityCapper.apply`.

    ``survivors`` is the kept subset (at most ``cap`` entries), in canonical
    sort order. ``dropped`` is ``len(candidates) - len(survivors)`` (0 when
    under cap). ``seen`` is the original candidate count, retained for the
    over-budget suggestion text.
    """

    survivors: list[tuple[dict[str, str], float]]
    dropped: int
    seen: int


@dataclass(frozen=True, slots=True)
class CardinalityCapper:
    """Deterministic per-tick cardinality cap. Pure and synchronous.

    Construct with the per-tick survivor budget; call :meth:`apply` with the
    family's candidate ``(labels, value)`` observations. A ``cap`` of 0 drops
    everything; a negative cap is treated as 0.
    """

    cap: int

    def apply(self, candidates: list[tuple[dict[str, str], float]]) -> CapResult:
        """Cap ``candidates`` to at most ``self.cap`` deterministic survivors.

        Sorts candidates by ``tuple(sorted(labels.items()))`` (a total order
        over label-sets) and takes the first ``cap``. Same candidate set ⇒ same
        survivors regardless of input order. Each list entry is treated as a
        distinct candidate; if two entries share an identical label-set, the
        stable sort keeps input order between them (collectors should not emit
        duplicate label-sets for one family).

        Args:
            candidates: ``(labels, value)`` pairs for one metric family.

        Returns:
            CapResult: survivors (<= cap), dropped count, seen count.
        """
        seen = len(candidates)
        effective_cap = max(self.cap, 0)
        ordered = sorted(candidates, key=lambda pair: tuple(sorted(pair[0].items())))
        survivors = ordered[:effective_cap]
        dropped = seen - len(survivors)
        return CapResult(survivors=survivors, dropped=dropped, seen=seen)


@dataclass(slots=True)
class CappedEmitter:
    """Bind a :class:`MetricsWriter` + a collector ``events`` list for capped emit.

    A collector constructs one per ``run()`` and calls :meth:`emit_family` once
    per metric family. ``emit_family`` returns the number of series written so
    the collector can accumulate ``CollectorResult.metrics_emitted``.
    """

    writer: MetricsWriter
    events: list[CollectorEvent]

    def emit_family(
        self,
        name: str,
        cap: int,
        observations: list[tuple[dict[str, str], float]],
    ) -> int:
        """Cap, emit survivors, record drops, and suggest on over-budget.

        Steps:
          1. Apply a :class:`CardinalityCapper` built with ``cap``.
          2. ``write_gauge(name, value, labels)`` for each survivor.
          3. ``write_gauge(M_FAMILY_DROPPED_SERIES, dropped, {"family": name})``
             ALWAYS — even when ``dropped == 0`` — so a family that recovers
             reports 0 instead of a stale/absent series.
          4. If ``dropped > 0``, append exactly ONE warning
             :class:`SuggestionEvent` naming the family, the cap, and the seen
             count.

        Args:
            name: the metric family (also the ``family`` label on the drop gauge).
            cap: the per-tick survivor budget for this family.
            observations: ``(labels, value)`` pairs for the family.

        Returns:
            int: the number of survivor series written (NOT counting the
            dropped-series gauge).
        """
        result = CardinalityCapper(cap=cap).apply(observations)
        for labels, value in result.survivors:
            self.writer.write_gauge(name, value, labels)
        self.writer.write_gauge(M_FAMILY_DROPPED_SERIES, float(result.dropped), {"family": name})
        if result.dropped > 0:
            self.events.append(
                SuggestionEvent(
                    title=f"Metric family {name} exceeded its cardinality cap",
                    body=(
                        f"metric {name} exceeded its {cap}-series budget "
                        f"({result.seen} seen); raise the cap or narrow the "
                        f"entity filter."
                    ),
                    severity="warning",
                )
            )
        return len(result.survivors)


__all__ = [
    "M_FAMILY_DROPPED_SERIES",
    "CapResult",
    "CappedEmitter",
    "CardinalityCapper",
]
