"""Shared test helpers for scheduler/plugin tests."""

from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter


def count_metric(
    metrics: InMemoryMetricsWriter,
    name: str,
    label_key: str | None = None,
    label_value: str | None = None,
) -> int:
    """Count metric entries by name with optional label filter."""
    matching = [e for e in metrics.recorded if e.name == name]
    if label_key is not None and label_value is not None:
        matching = [e for e in matching if e.labels.get(label_key) == label_value]
    return len(matching)
