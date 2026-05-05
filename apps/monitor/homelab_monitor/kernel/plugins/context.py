"""The :class:`CollectorContext` injected into every collector run.

Carries the 9 kernel handles a collector may need (per spec §5.2). ``slots=True``
keeps it small + pickle-safe for ``RunKind.PROCESS`` collectors. ``ha`` is the only
optional field and therefore is declared last (slots-dataclass field-default rule).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from structlog import BoundLogger

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.plugins.io import (
    HomeAssistantClient,
    LogsWriter,
    MetricsWriter,
    SshClientFactory,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver


@dataclass(slots=True)
class CollectorContext:
    """Runtime context handed to a collector's ``run`` coroutine.

    All fields are required EXCEPT ``ha`` (None when the collector does not target
    Home Assistant). Fields are listed in spec §5.2 order; ``ha`` is moved to the
    end so the slots-dataclass field-default ordering rule is satisfied.
    """

    config: CollectorConfig
    db: SqliteRepository
    vm: MetricsWriter
    vl: LogsWriter
    http: httpx.AsyncClient
    ssh: SshClientFactory
    secrets: SyncSecretsResolver
    log: BoundLogger
    ha: HomeAssistantClient | None = None
