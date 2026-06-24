"""Synology integration collector bundle (EPIC-008).

Exposes :func:`register_all`, which the FastAPI lifespan calls once at startup.
Each collector class is registered with per-collector failure isolation (one bad
register does not abort the rest), mirroring the unifi bundle exemplar
(EPIC-007 STAGE-007-002).

Future Wave-B stages (STAGE-008-005+) append their device collector classes to
``_SYNOLOGY_COLLECTORS`` — that 1-line edit is the whole wiring step.
"""

from __future__ import annotations

import structlog

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.loader import PluginLoader, config_from_classvars
from homelab_monitor.plugins.collectors.integrations.synology.backup import (
    SynologyBackupCollector,
)
from homelab_monitor.plugins.collectors.integrations.synology.pool import (
    SynologyPoolCollector,
)
from homelab_monitor.plugins.collectors.integrations.synology.replication import (
    SynologyReplicationCollector,
)
from homelab_monitor.plugins.collectors.integrations.synology.storage import (
    SynologyStorageCollector,
)
from homelab_monitor.plugins.collectors.integrations.synology.system import (
    SynologySystemCollector,
)
from homelab_monitor.plugins.collectors.integrations.synology.ups import (
    SynologyUPSCollector,
)
from homelab_monitor.plugins.collectors.integrations.synology.utilization import (
    SynologyUtilizationCollector,
)

_log = structlog.get_logger()

_SYNOLOGY_COLLECTORS: list[type[BaseCollector]] = [
    SynologyStorageCollector,
    SynologyPoolCollector,
    SynologySystemCollector,
    SynologyUtilizationCollector,
    SynologyUPSCollector,
    SynologyBackupCollector,
    SynologyReplicationCollector,
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
