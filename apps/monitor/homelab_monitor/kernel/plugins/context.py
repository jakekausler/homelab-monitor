"""The :class:`CollectorContext` injected into every collector run.

Carries the 9 kernel handles a collector may need (per spec §5.2). ``slots=True``
keeps it small + pickle-safe for ``RunKind.PROCESS`` collectors. ``ha`` is the only
optional field and therefore is declared last (slots-dataclass field-default rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from structlog import BoundLogger

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.plugins.io import (
    HomeAssistantClient,
    LogsWriter,
    MetricsWriter,
    PiholeClient,
    SshClientFactory,
    UnifiClient,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver

if TYPE_CHECKING:
    from homelab_monitor.kernel.ha.entity_registry import HaEntityRegistryCache


@dataclass(slots=True)
class CollectorContext:
    """Runtime context handed to a collector's ``run`` coroutine.

    All fields are required EXCEPT the integration handles ``ha``, ``ha_registry``,
    ``unifi`` and ``pihole`` (each None when the collector does not target that
    integration). The optional handles are listed last so the slots-dataclass
    field-default ordering rule is satisfied.
    """

    config: CollectorConfig
    db: SqliteRepository
    vm: MetricsWriter
    vl: LogsWriter
    # TODO: consider abstracting behind Protocol when STAGE-001-015 introduces test doubles
    http: httpx.AsyncClient
    ssh: SshClientFactory
    secrets: SyncSecretsResolver
    log: BoundLogger
    ha: HomeAssistantClient | None = None
    ha_registry: HaEntityRegistryCache | None = None
    unifi: UnifiClient | None = None
    pihole: PiholeClient | None = None
