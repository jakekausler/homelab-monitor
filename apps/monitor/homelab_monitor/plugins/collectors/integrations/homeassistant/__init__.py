"""Home Assistant integration collector bundle (EPIC-005 exemplar).

Exposes :func:`register_all`, which the FastAPI lifespan calls once at startup.
Each collector class is registered with per-collector failure isolation (one
bad register does not abort the rest), mirroring the builtin-collector
registration loop in ``kernel/api/lifespan.py``.

Wave-B stages (STAGE-005-006+) append their collector class to
``_HA_COLLECTORS`` — that 1-line edit is the whole wiring step.
"""

from __future__ import annotations

import structlog

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.loader import PluginLoader, config_from_classvars
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_battery import (
    HaBatteryCollector,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_cadence import (
    HaCadenceCollector,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_config_entry import (
    HaConfigEntryCollector,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_entity_available import (
    HaEntityAvailableCollector,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_up import (
    HaUpCollector,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_update import (
    HaUpdateCollector,
)

_log = structlog.get_logger()

# Wave-B stages append their collector class here.
_HA_COLLECTORS: list[type[BaseCollector]] = [
    HaUpCollector,
    HaEntityAvailableCollector,
    HaBatteryCollector,
    HaUpdateCollector,
    HaCadenceCollector,
    HaConfigEntryCollector,
]


def register_all(loader: PluginLoader) -> None:
    """Register every Home Assistant collector with per-collector isolation.

    Each class is registered under its own ``try/except`` so a single failing
    ``register`` (e.g. a bad ClassVar / config) is logged and skipped without
    aborting the rest of the bundle — the same failure-isolation policy the
    lifespan applies to builtin collectors.
    """
    for cls in _HA_COLLECTORS:
        try:
            loader.register(cls, config_from_classvars(cls))
        except Exception as exc:
            _log.warning(
                "ha_integration.collector_register_failed",
                name=cls.name,
                error=str(exc),
            )
