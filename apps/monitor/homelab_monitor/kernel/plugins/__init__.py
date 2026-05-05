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
from homelab_monitor.kernel.plugins.manifest import SubprocessManifest
from homelab_monitor.kernel.plugins.noop import NoopCollector
from homelab_monitor.kernel.plugins.process_context import (
    BufferingMetricsWriter,
    ProcessCollectorContext,
)
from homelab_monitor.kernel.plugins.subprocess_collector import make_subprocess_collector
from homelab_monitor.kernel.plugins.subprocess_runner import (
    SIGTERM_GRACE_SECONDS,
    run_subprocess,
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
    "SIGTERM_GRACE_SECONDS",
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
    "SubprocessManifest",
    "SuggestionEvent",
    "TrustLevel",
    "make_subprocess_collector",
    "run_subprocess",
]
