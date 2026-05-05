"""Plugin layer: Collector Protocol, BaseCollector ABC, context, events, IO Protocols.

Public surface for in-process Python collectors. Subprocess plugins (STAGE-001-009)
satisfy the same Protocol structurally over a JSON-RPC bridge.
"""

from __future__ import annotations

from homelab_monitor.kernel.plugins.base import BaseCollector, Collector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    HomeAssistantClient,
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
    LogEntry,
    LogsWriter,
    MetricEntry,
    MetricsWriter,
    SshClientFactory,
    SshConnection,
)
from homelab_monitor.kernel.plugins.loader import LoadedCollector, PluginLoader
from homelab_monitor.kernel.plugins.noop import NoopCollector
from homelab_monitor.kernel.plugins.process_context import (
    BufferingMetricsWriter,
    ProcessCollectorContext,
)
from homelab_monitor.kernel.plugins.types import (
    AlertForwardEvent,
    CollectorConfig,
    CollectorEvent,
    CollectorResult,
    HeartbeatEvent,
    LogSignatureEvent,
    RunKind,
    SuggestionEvent,
    TrustLevel,
)

__all__ = [
    "AlertForwardEvent",
    "BaseCollector",
    "BufferingMetricsWriter",
    "Collector",
    "CollectorConfig",
    "CollectorContext",
    "CollectorEvent",
    "CollectorResult",
    "HeartbeatEvent",
    "HomeAssistantClient",
    "InMemoryLogsWriter",
    "InMemoryMetricsWriter",
    "LoadedCollector",
    "LogEntry",
    "LogSignatureEvent",
    "LogsWriter",
    "MetricEntry",
    "MetricsWriter",
    "NoopCollector",
    "PluginLoader",
    "ProcessCollectorContext",
    "RunKind",
    "SshClientFactory",
    "SshConnection",
    "SuggestionEvent",
    "TrustLevel",
]
