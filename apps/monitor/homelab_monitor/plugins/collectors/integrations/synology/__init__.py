"""Synology integration collector bundle (EPIC-008).

Exposes :func:`register_all`, which the FastAPI lifespan calls once at startup.
Each collector class is registered with per-collector failure isolation (one bad
register does not abort the rest), mirroring the unifi bundle exemplar
(EPIC-007 STAGE-007-002).

Wave-B stages (STAGE-008-005+) append their device collector classes to
``_SYNOLOGY_COLLECTORS`` — that 1-line edit is the whole wiring step — and remove
the placeholder scaffolding (see below).
"""

from __future__ import annotations

import structlog

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.loader import PluginLoader, config_from_classvars
from homelab_monitor.plugins.collectors.integrations.synology.placeholder import (
    SynologyPlaceholderCollector,
)

_log = structlog.get_logger()

# SCAFFOLDING: removed in STAGE-008-005
_SYNOLOGY_COLLECTORS: list[type[BaseCollector]] = [
    SynologyPlaceholderCollector,
]


def register_all(loader: PluginLoader) -> None:
    """Register every Synology collector with per-collector isolation.

    Each class is registered under its own ``try/except`` so a single failing
    ``register`` (e.g. a bad ClassVar / config) is logged and skipped without
    aborting the rest of the bundle — the same failure-isolation policy the
    lifespan applies to builtin collectors.
    """
    for cls in _SYNOLOGY_COLLECTORS:
        try:
            loader.register(cls, config_from_classvars(cls))
        except Exception as exc:
            _log.warning(
                "synology_integration.collector_register_failed",
                name=cls.name,
                error=str(exc),
            )
