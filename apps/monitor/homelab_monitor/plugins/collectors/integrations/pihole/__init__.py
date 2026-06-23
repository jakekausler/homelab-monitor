"""Pi-hole integration collector bundle (EPIC-006).

Exposes :func:`register_all`, which the FastAPI lifespan calls once at startup.
Each collector class is registered with per-collector failure isolation (one bad
register does not abort the rest), mirroring the homeassistant bundle exemplar
(EPIC-005 STAGE-005-003) and the unifi bundle (EPIC-007 STAGE-007-002).

Wave-B stages (STAGE-006-005+) append their collector classes to
``_PIHOLE_COLLECTORS`` — that 1-line edit is the whole wiring step.
"""

from __future__ import annotations

import structlog

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.loader import PluginLoader, config_from_classvars
from homelab_monitor.plugins.collectors.integrations.pihole.blocking import (
    PiholeBlockingCollector,
)
from homelab_monitor.plugins.collectors.integrations.pihole.ftl_health import (
    PiholeFtlHealthCollector,
)
from homelab_monitor.plugins.collectors.integrations.pihole.ftl_messages import (
    PiholeFtlMessagesCollector,
)
from homelab_monitor.plugins.collectors.integrations.pihole.gravity import (
    PiholeGravityCollector,
)
from homelab_monitor.plugins.collectors.integrations.pihole.stats_summary import (
    PiholeStatsSummaryCollector,
)
from homelab_monitor.plugins.collectors.integrations.pihole.top_clients import (
    PiholeClientsCollector,
)
from homelab_monitor.plugins.collectors.integrations.pihole.upstreams import (
    PiholeUpstreamsCollector,
)
from homelab_monitor.plugins.collectors.integrations.pihole.version import (
    PiholeVersionCollector,
)

_log = structlog.get_logger()

# Wave-B stages append their collector class here.
_PIHOLE_COLLECTORS: list[type[BaseCollector]] = [
    PiholeBlockingCollector,
    PiholeFtlHealthCollector,
    PiholeFtlMessagesCollector,
    PiholeStatsSummaryCollector,
    PiholeUpstreamsCollector,
    PiholeGravityCollector,
    PiholeVersionCollector,
    PiholeClientsCollector,
]


def register_all(loader: PluginLoader) -> None:
    """Register every Pi-hole collector with per-collector isolation.

    Each class is registered under its own ``try/except`` so a single failing
    ``register`` (e.g. a bad ClassVar / config) is logged and skipped without
    aborting the rest of the bundle — the same failure-isolation policy the
    lifespan applies to builtin collectors.
    """
    for cls in _PIHOLE_COLLECTORS:
        try:
            loader.register(cls, config_from_classvars(cls))
        except Exception as exc:
            _log.warning(
                "pihole_integration.collector_register_failed",
                name=cls.name,
                error=str(exc),
            )
