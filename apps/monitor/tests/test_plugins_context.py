"""Tests for CollectorContext: dataclass with slots, optional ha, replaceable."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import cast

import httpx
import pytest
import pytest_asyncio
import structlog
from structlog import BoundLogger

from homelab_monitor.kernel.db.repository import SqliteRepository
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.io import (
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


@pytest_asyncio.fixture
async def ctx() -> AsyncIterator[CollectorContext]:
    """A CollectorContext built with a properly-closed httpx.AsyncClient."""
    async with httpx.AsyncClient() as http:
        engine = cast(SqliteRepository, object())  # opaque stub; not used by these tests
        log = cast(BoundLogger, structlog.get_logger())
        yield CollectorContext(
            config=CollectorConfig(name="ctx-test"),
            db=engine,
            vm=InMemoryMetricsWriter(),
            vl=InMemoryLogsWriter(),
            http=http,
            ssh=_StubFactory(),
            secrets=SyncSecretsResolver(),
            log=log,
            ha=None,
        )


async def test_context_constructs_with_all_fields(ctx: CollectorContext) -> None:
    """All 9 fields can be supplied; ha defaults to None."""
    assert ctx.config.name == "ctx-test"
    assert ctx.ha is None


async def test_context_accepts_ha_value() -> None:
    """ha may be a HomeAssistantClient (any object satisfies the empty Protocol)."""

    class _FakeHa:
        pass

    async with httpx.AsyncClient() as http:
        engine = cast(SqliteRepository, object())
        log = cast(BoundLogger, structlog.get_logger())
        ctx = CollectorContext(
            config=CollectorConfig(name="ctx-test"),
            db=engine,
            vm=InMemoryMetricsWriter(),
            vl=InMemoryLogsWriter(),
            http=http,
            ssh=_StubFactory(),
            secrets=SyncSecretsResolver(),
            log=log,
            ha=_FakeHa(),
        )
        assert ctx.ha is not None


async def test_context_has_slots(ctx: CollectorContext) -> None:
    """CollectorContext is a slots dataclass — arbitrary attributes are rejected."""
    assert hasattr(CollectorContext, "__slots__")
    with pytest.raises(AttributeError):
        ctx.surprise = 1  # type: ignore[attr-defined]


async def test_context_replace_returns_new_instance_with_overrides(ctx: CollectorContext) -> None:
    """``dataclasses.replace`` works on a slots dataclass; original is untouched."""
    new_cfg = CollectorConfig(name="other")
    replaced = dataclasses.replace(ctx, config=new_cfg)
    assert replaced.config.name == "other"
    assert ctx.config.name == "ctx-test"
