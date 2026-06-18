"""Shared numeric-parsing + emit helpers for the Unifi integration collectors.

These three helpers are reused by every Unifi collector (device, wan, and the
Wave-B/C collectors that follow). They live here -- in the integrations package,
not the kernel -- because ``emit_numeric`` depends on ``CollectorContext`` (a
plugin-layer type); keeping them in the plugin layer respects the kernel boundary.

- ``as_float`` -- parse int/float/numeric-string to float, returning None on failure.
- ``as_bool`` -- return a bool value if the input is a bool, else False.
- ``emit_numeric`` -- parse via ``as_float`` and ``write_gauge`` if not None.
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


def as_bool(v: object) -> bool:
    """Return bool value if v is a bool, otherwise False."""
    if isinstance(v, bool):
        return v
    return False


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
