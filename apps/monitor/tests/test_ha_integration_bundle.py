"""Tests for config_from_classvars + the homeassistant integration bundle."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.loader import (
    LoadedCollector,
    PluginLoader,
    config_from_classvars,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant import register_all
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_up import (
    HaUpCollector,
)

_EXPECTED_INTERVAL = 30
_EXPECTED_TIMEOUT = 10


def test_config_from_classvars_derives_dict() -> None:
    """Derives name + interval_seconds + timeout_seconds from ClassVars."""
    cfg = config_from_classvars(HaUpCollector)
    assert cfg == {
        "name": "ha_up",
        "interval_seconds": _EXPECTED_INTERVAL,
        "timeout_seconds": _EXPECTED_TIMEOUT,
    }


def test_config_from_classvars_layers_overrides() -> None:
    """Extra kwargs are layered onto the derived dict."""
    cfg = config_from_classvars(HaUpCollector, enabled=False)
    assert cfg["enabled"] is False
    assert cfg["name"] == "ha_up"


def test_config_from_classvars_override_wins() -> None:
    """An override key replaces the derived value."""
    cfg = config_from_classvars(HaUpCollector, interval_seconds=99)
    assert cfg["interval_seconds"] == 99  # noqa: PLR2004


def test_register_all_registers_ha_up() -> None:
    """register_all registers HaUpCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    assert len(loaded) == 1
    record = loaded[0]
    assert isinstance(record, LoadedCollector)
    assert isinstance(record.collector, HaUpCollector)
    assert record.config.name == "ha_up"
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
