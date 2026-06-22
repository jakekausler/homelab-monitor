"""Numeric-parsing + emit helpers for the Pi-hole integration collectors.

Copied from homelab_monitor.plugins.collectors.integrations.unifi._parsing — kept
here (not cross-imported) to avoid coupling between integration bundles.

- ``as_float``    -- parse int/float/numeric-string to float, returning None on failure.
- ``emit_numeric`` -- parse via as_float and write_gauge if not None.
"""

from __future__ import annotations

from homelab_monitor.kernel.plugins.context import CollectorContext


def as_float(v: object) -> float | None:
    """Parse int, float, or numeric string to float. Returns None for bool, non-numeric, None.

    bool must be excluded FIRST because ``isinstance(True, int)`` is True in Python.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def emit_numeric(
    ctx: CollectorContext,
    name: str,
    value_obj: object,
    labels: dict[str, str],
    emitted: list[int],
) -> None:
    """Parse value_obj via as_float and write_gauge if not None; increment emitted[0]."""
    val = as_float(value_obj)
    if val is not None:
        ctx.vm.write_gauge(name, val, labels)
        emitted[0] += 1
