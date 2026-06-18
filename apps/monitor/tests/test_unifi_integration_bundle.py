"""Tests for the unifi integration bundle register_all (STAGE-007-002)."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.loader import LoadedCollector, PluginLoader
from homelab_monitor.plugins.collectors.integrations.unifi import register_all
from homelab_monitor.plugins.collectors.integrations.unifi.placeholder import (
    UnifiPlaceholderCollector,
)

_EXPECTED_INTERVAL = 60
_EXPECTED_TIMEOUT = 5


def test_register_all_registers_placeholder() -> None:
    """register_all registers UnifiPlaceholderCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, UnifiPlaceholderCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "unifi_placeholder"
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
