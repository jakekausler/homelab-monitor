"""Tests for CollectorContext: dataclass with slots, optional ha, replaceable."""

from __future__ import annotations

import dataclasses
from contextlib import AbstractAsyncContextManager
from typing import cast

import httpx
import pytest
import structlog
from structlog import BoundLogger

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
    HomeAssistantClient,
    InMemoryLogsWriter,
    InMemoryMetricsWriter,
    SshConnection,
)
from homelab_monitor.kernel.plugins.types import CollectorConfig
from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver


class _StubFactory:
    def open(self, target_id: str) -> AbstractAsyncContextManager[SshConnection]:
        del target_id
        raise NotImplementedError


def _make_ctx(*, ha: HomeAssistantClient | None = None) -> CollectorContext:
    """Construct a CollectorContext with stubs for every field."""
    engine = cast(SqliteRepository, object())  # opaque stub; not used by these tests
    log = cast(BoundLogger, structlog.get_logger())
    return CollectorContext(
        config=CollectorConfig(name="ctx-test"),
        db=engine,
        vm=InMemoryMetricsWriter(),
        vl=InMemoryLogsWriter(),
        http=httpx.AsyncClient(),
        ssh=_StubFactory(),
        secrets=SyncSecretsResolver(),
        log=log,
        ha=ha,
    )


def test_context_constructs_with_all_fields() -> None:
    """All 9 fields can be supplied; ha defaults to None."""
    ctx = _make_ctx()
    assert ctx.config.name == "ctx-test"
    assert ctx.ha is None


def test_context_accepts_ha_value() -> None:
    """ha may be a HomeAssistantClient (any object satisfies the empty Protocol)."""

    class _FakeHa:
        pass

    ctx = _make_ctx(ha=_FakeHa())
    assert ctx.ha is not None


def test_context_has_slots() -> None:
    """CollectorContext is a slots dataclass — arbitrary attributes are rejected."""
    ctx = _make_ctx()
    assert hasattr(CollectorContext, "__slots__")
    with pytest.raises(AttributeError):
        ctx.surprise = 1  # type: ignore[attr-defined]


def test_context_replace_returns_new_instance_with_overrides() -> None:
    """``dataclasses.replace`` works on a slots dataclass; original is untouched."""
    ctx = _make_ctx()
    new_cfg = CollectorConfig(name="other")
    replaced = dataclasses.replace(ctx, config=new_cfg)
    assert replaced.config.name == "other"
    assert ctx.config.name == "ctx-test"
