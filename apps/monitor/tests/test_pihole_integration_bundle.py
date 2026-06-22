"""Tests for the pihole integration bundle register_all (STAGE-006-002)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.loader import LoadedCollector, PluginLoader
from homelab_monitor.plugins.collectors.integrations.pihole import register_all
from homelab_monitor.plugins.collectors.integrations.pihole.placeholder import (
    PiholePlaceholderCollector,
)

_EXPECTED_INTERVAL = 60
_EXPECTED_TIMEOUT = 5


def test_register_all_registers_placeholder() -> None:
    """register_all registers PiholePlaceholderCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, PiholePlaceholderCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "pihole_placeholder"
    assert record.config.interval_seconds == _EXPECTED_INTERVAL
    assert record.config.timeout_seconds == _EXPECTED_TIMEOUT


def test_register_all_isolates_failing_register(monkeypatch: pytest.MonkeyPatch) -> None:
    """A register() that raises is logged and does NOT propagate."""
    loader = PluginLoader()

    def _boom(cls: type[BaseCollector], overrides: dict[str, object] | None = None) -> object:
        raise RuntimeError("synthetic register failure")

    monkeypatch.setattr(loader, "register", _boom)
    # Must not raise — register_all swallows + logs the per-collector failure.
    register_all(loader)
    # Nothing got registered because the only collector failed.
    assert loader.load_all() == []


@pytest.mark.asyncio
async def test_placeholder_run_emits_bundle_loaded() -> None:
    """run() emits homelab_pihole_bundle_loaded=1.0 and returns ok=True, metrics_emitted=1."""
    collector = PiholePlaceholderCollector()
    ctx = MagicMock()
    ctx.vm.write_gauge = MagicMock()

    result = await collector.run(ctx)

    assert result.ok is True
    assert result.metrics_emitted == 1
    assert result.errors == []
    assert result.events == []
    assert result.duration_seconds >= 0.0
    ctx.vm.write_gauge.assert_called_once_with("homelab_pihole_bundle_loaded", 1.0, {})
