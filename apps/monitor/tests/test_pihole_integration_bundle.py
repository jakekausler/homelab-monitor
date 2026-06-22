"""Tests for the pihole integration bundle register_all (STAGE-006-005)."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.loader import LoadedCollector, PluginLoader
from homelab_monitor.plugins.collectors.integrations.pihole import register_all
from homelab_monitor.plugins.collectors.integrations.pihole.stats_summary import (
    PiholeStatsSummaryCollector,
)

_EXPECTED_INTERVAL = 30
_EXPECTED_TIMEOUT = 15


def test_register_all_registers_stats_summary() -> None:
    """register_all registers PiholeStatsSummaryCollector with the correct derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, PiholeStatsSummaryCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "pihole_stats_summary"
    assert record.config.interval_seconds == _EXPECTED_INTERVAL
    assert record.config.timeout_seconds == _EXPECTED_TIMEOUT


def test_register_all_isolates_failing_register(monkeypatch: pytest.MonkeyPatch) -> None:
    """A register() that raises is logged and does NOT propagate."""
    loader = PluginLoader()

    def _boom(cls: type[BaseCollector], overrides: dict[str, object] | None = None) -> object:
        raise RuntimeError("synthetic register failure")

    monkeypatch.setattr(loader, "register", _boom)
    register_all(loader)
    assert loader.load_all() == []
