"""Unifi integration collector bundle (EPIC-007).

Exposes :func:`register_all`, which the FastAPI lifespan calls once at startup.
Each collector class is registered with per-collector failure isolation (one bad
register does not abort the rest), mirroring the homeassistant bundle exemplar
(EPIC-005 STAGE-005-003).

Wave-B stages (STAGE-007-005+) append their device collector classes to
``_UNIFI_COLLECTORS`` — that 1-line edit is the whole wiring step — and remove
the placeholder scaffolding (see below).
"""

from __future__ import annotations

import structlog

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.loader import PluginLoader, config_from_classvars
from homelab_monitor.plugins.collectors.integrations.unifi.placeholder import (
    UnifiPlaceholderCollector,
)

_log = structlog.get_logger()

# Wave-B stages append their collector class here.
_UNIFI_COLLECTORS: list[type[BaseCollector]] = [
    # SCAFFOLDING (STAGE-007-002): throwaway no-op proving the bundle loads.
    # REMOVED by STAGE-007-005 (first Wave-B device collector) — delete this
    # entry AND placeholder.py.
    UnifiPlaceholderCollector,
]


def register_all(loader: PluginLoader) -> None:
    """Register every Unifi collector with per-collector isolation.

    Each class is registered under its own ``try/except`` so a single failing
    ``register`` (e.g. a bad ClassVar / config) is logged and skipped without
    aborting the rest of the bundle — the same failure-isolation policy the
    lifespan applies to builtin collectors.
    """
    for cls in _UNIFI_COLLECTORS:
        try:
            loader.register(cls, config_from_classvars(cls))
        except Exception as exc:
            _log.warning(
                "unifi_integration.collector_register_failed",
                name=cls.name,
                error=str(exc),
            )
