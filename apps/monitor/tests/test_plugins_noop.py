"""Tests for the NoopCollector proof-of-concept."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import cast

import httpx
import structlog
from structlog import BoundLogger

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.plugins.base import Collector
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
    SshConnection,
)
from homelab_monitor.kernel.plugins.noop import NoopCollector
from homelab_monitor.kernel.plugins.types import (
    CollectorConfig,
    CollectorResult,
    RunKind,
    TrustLevel,
)
from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver


class _StubFactory:
    def open(self, target_id: str) -> AbstractAsyncContextManager[SshConnection]:
        del target_id
        raise NotImplementedError


def _make_ctx() -> CollectorContext:
    log = cast(BoundLogger, structlog.get_logger())
    return CollectorContext(
        config=CollectorConfig(name="noop"),
        db=cast(SqliteRepository, object()),
        vm=InMemoryMetricsWriter(),
        vl=InMemoryLogsWriter(),
        http=httpx.AsyncClient(),
        ssh=_StubFactory(),
        secrets=SyncSecretsResolver(),
        log=log,
        ha=None,
    )


def test_noop_class_attributes() -> None:
    """NoopCollector advertises its identity via ClassVars."""
    assert NoopCollector.name == "noop"
    assert NoopCollector.run_kind is RunKind.ASYNC
    assert NoopCollector.trust_level is TrustLevel.BUILTIN


def test_noop_is_instantiable() -> None:
    """NoopCollector overrides ``run`` so it is not abstract."""
    assert isinstance(NoopCollector(), NoopCollector)


def test_noop_satisfies_collector_protocol() -> None:
    """NoopCollector is assignable to the Collector Protocol."""
    c: Collector = NoopCollector()
    assert c.name == "noop"


async def test_noop_run_returns_empty_successful_result() -> None:
    """``run`` returns ok=True with zero metrics, zero events, zero errors."""
    result = await NoopCollector().run(_make_ctx())
    assert isinstance(result, CollectorResult)
    assert result.ok is True
    assert result.metrics_emitted == 0
    assert result.errors == []
    assert result.events == []
    assert result.duration_seconds == 0.0
