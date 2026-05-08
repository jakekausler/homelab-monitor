"""MetricsWriter backed by a ``prometheus_client.CollectorRegistry``.

Lazily registers metrics on first emit, keyed by ``(name, frozenset(labelnames))``.
Subsequent emits with the same metric name + identical label-name set look up
the cached metric and call ``.labels(**labels).set/inc/observe``. Emits with a
DIFFERENT label-name set for the same metric name are logged at WARNING and
silently skipped — Prometheus semantics forbid re-registering a metric with
varying label-name sets, so the writer treats the first-seen labelnames as
authoritative.

``replace_family(name, entries)`` clears all label-set children of the
matching metric and re-emits each entry as a gauge.
"""

from __future__ import annotations

from typing import cast

import structlog
from prometheus_client import CollectorRegistry, Counter, Gauge, Summary
from prometheus_client.metrics import MetricWrapperBase
from structlog.stdlib import BoundLogger


class PrometheusRegistryWriter:
    """Implements :class:`MetricsWriter` against a ``prometheus_client`` registry."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self._registry = registry
        # Cache: metric_name -> (labelnames_frozenset, MetricWrapperBase)
        # NOT keyed by labelnames because Prometheus only allows ONE
        # registration per metric name; cross-labelname-set emits are an
        # error. We keep the labelnames so we can log+skip mismatches.
        self._metrics: dict[str, tuple[frozenset[str], MetricWrapperBase]] = {}
        self._log: BoundLogger = cast(
            BoundLogger,
            structlog.get_logger().bind(component="prometheus_writer"),
        )

    def _get_or_create(
        self,
        kind: str,
        name: str,
        labels: dict[str, str],
    ) -> MetricWrapperBase | None:
        """Look up or register a metric. Returns None on labelname mismatch."""
        labelnames = frozenset(labels.keys())
        cached = self._metrics.get(name)
        if cached is not None:
            existing_names, existing_metric = cached
            if existing_names != labelnames:
                self._log.warning(
                    "prometheus_writer.labelnames_mismatch",
                    metric=name,
                    expected=sorted(existing_names),
                    got=sorted(labelnames),
                )
                return None
            return existing_metric

        # First time seeing this metric name: register it.
        labelnames_tuple = tuple(sorted(labelnames))
        metric: MetricWrapperBase
        if kind == "gauge":
            metric = Gauge(
                name,
                f"homelab-monitor gauge: {name}",
                labelnames=labelnames_tuple,
                registry=self._registry,
            )
        elif kind == "counter":
            metric = Counter(
                name,
                f"homelab-monitor counter: {name}",
                labelnames=labelnames_tuple,
                registry=self._registry,
            )
        else:  # kind == "summary"
            metric = Summary(
                name,
                f"homelab-monitor summary: {name}",
                labelnames=labelnames_tuple,
                registry=self._registry,
            )
        self._metrics[name] = (labelnames, metric)
        return metric

    def write_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Set a gauge value (registers the gauge on first emit)."""
        metric = self._get_or_create("gauge", name, labels)
        if metric is None:
            return
        gauge = cast(Gauge, metric)
        if labels:
            gauge.labels(**labels).set(value)
        else:
            gauge.set(value)

    def write_counter(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Increment a counter by ``value`` (must be non-negative)."""
        metric = self._get_or_create("counter", name, labels)
        if metric is None:
            return
        counter = cast(Counter, metric)
        if labels:
            counter.labels(**labels).inc(value)
        else:
            counter.inc(value)

    def write_summary(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Observe a summary sample."""
        metric = self._get_or_create("summary", name, labels)
        if metric is None:
            return
        summary = cast(Summary, metric)
        if labels:
            summary.labels(**labels).observe(value)
        else:
            summary.observe(value)

    def replace_family(self, name: str, entries: list[tuple[float, dict[str, str]]]) -> None:
        """Clear all label-set children of ``name`` and re-emit the entries.

        If the metric has not been registered yet, treats this as the initial
        registration via the first entry's labelnames. If the metric exists
        and any entry has mismatching labelnames, that entry is dropped (with
        a warning) — the OTHER entries still flow.
        """
        cached = self._metrics.get(name)
        if cached is not None:
            _, metric = cached
            metric.clear()
        for value, labels in entries:
            self.write_gauge(name, value, labels)
