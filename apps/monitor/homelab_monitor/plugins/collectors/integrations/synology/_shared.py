"""Shared helper layer for Synology collectors (Waves B/C/E, STAGE-008-005+).

Provides:
- Error-result constructors for unconfigured client and DSM errors
- fetch_or_result: latency emission + error short-circuit
- Numeric field parsers (as_float, bytes_field, percent_field)
- Cardinality helpers (cap_for_synology, capped_emitter)

fetch_or_result returns the SynologyResponse unchanged on success — the collector owns the
degraded-payload decision (the ok=True-when-NAS-sad convention); only a client-level
SynologyError maps to a failed (ok=False) CollectorResult.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Final

from homelab_monitor.kernel.metrics.cardinality import CappedEmitter
from homelab_monitor.kernel.plugins.types import CollectorResult
from homelab_monitor.kernel.synology.errors import SynologyError

if TYPE_CHECKING:
    from homelab_monitor.kernel.plugins.context import CollectorContext
    from homelab_monitor.kernel.plugins.types import CollectorEvent
    from homelab_monitor.kernel.synology.client import SynologyResponse

M_API_TOOK_SECONDS: Final[str] = "homelab_synology_api_took_seconds"


def client_unconfigured_result(start: float) -> CollectorResult:
    return CollectorResult(
        ok=False,
        metrics_emitted=0,
        errors=["synology client not configured"],
        events=[],
        duration_seconds=time.monotonic() - start,
    )


def failed_result(error: SynologyError, start: float) -> CollectorResult:
    return CollectorResult(
        ok=False,
        metrics_emitted=0,
        errors=[error.message],
        events=[],
        duration_seconds=time.monotonic() - start,
    )


def fetch_or_result(
    ctx: CollectorContext,
    response: SynologyResponse | SynologyError,
    start: float,
    emitted: list[int],
) -> SynologyResponse | CollectorResult:
    if isinstance(response, SynologyError):
        return failed_result(response, start)
    ctx.vm.write_gauge(
        M_API_TOOK_SECONDS,
        response.took_seconds,
        {"api": response.endpoint},
    )
    emitted[0] += 1
    return response


def as_float(v: object) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        val = float(v)
        return val if math.isfinite(val) else None
    if isinstance(v, str):
        try:
            val = float(v.strip())
        except ValueError:
            return None
        return val if math.isfinite(val) else None
    return None


# SCAFFOLDING: consumed in STAGE-008-005/006/009
def bytes_field(v: object) -> float | None:
    """Parse a DSM byte-count field to float bytes, or None if unparseable."""
    return as_float(v)


# SCAFFOLDING: consumed in STAGE-008-005/006/009
def percent_field(v: object) -> float | None:
    """Parse a DSM percent field (0-100 numeric or numeric string) to float, or None."""
    return as_float(v)


# SCAFFOLDING: consumed in STAGE-008-005/006/009
def cap_for_synology(family: str) -> int:
    """Return the cardinality cap for a Synology metric family (default 500)."""
    from homelab_monitor.kernel.config import load_cardinality_caps_config  # noqa: PLC0415

    return load_cardinality_caps_config().cap_for(family)


# SCAFFOLDING: consumed in STAGE-008-005/006/009
def capped_emitter(ctx: CollectorContext, events: list[CollectorEvent]) -> CappedEmitter:
    """Construct a CappedEmitter wired to ctx.vm for Synology collectors."""
    return CappedEmitter(writer=ctx.vm, events=events)
