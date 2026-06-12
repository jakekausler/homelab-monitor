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
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_anomaly_zscore import (
    HaAnomalyZscoreCollector,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_battery import (
    HaBatteryCollector,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_cadence import (
    HaCadenceCollector,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_config_entry import (
    HaConfigEntryCollector,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_entity_available import (
    HaEntityAvailableCollector,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_persistent_notification import (  # noqa: E501
    HaPersistentNotificationCollector,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_repairs import (
    HaRepairsCollector,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_safety_sensors import (
    HaSafetySensorsCollector,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_sensor_value import (
    HaSensorValueCollector,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_up import (
    HaUpCollector,
)
from homelab_monitor.plugins.collectors.integrations.homeassistant.ha_update import (
    HaUpdateCollector,
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
    ha_up_records = [r for r in loaded if isinstance(r.collector, HaUpCollector)]
    assert len(ha_up_records) == 1
    record = ha_up_records[0]
    assert isinstance(record, LoadedCollector)
    assert isinstance(record.collector, HaUpCollector)
    assert record.config.name == "ha_up"
    assert record.config.interval_seconds == _EXPECTED_INTERVAL
    assert record.config.timeout_seconds == _EXPECTED_TIMEOUT


def test_register_all_registers_ha_entity_available() -> None:
    """register_all registers HaEntityAvailableCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, HaEntityAvailableCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "ha_entity_available"


def test_register_all_registers_ha_battery() -> None:
    """register_all registers HaBatteryCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, HaBatteryCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "ha_battery"


def test_register_all_registers_ha_update() -> None:
    """register_all registers HaUpdateCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, HaUpdateCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "ha_update"


def test_register_all_registers_ha_cadence() -> None:
    """register_all registers HaCadenceCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, HaCadenceCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "ha_cadence"


def test_register_all_registers_ha_config_entry() -> None:
    """register_all registers HaConfigEntryCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, HaConfigEntryCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "ha_config_entry"


def test_register_all_registers_ha_repairs() -> None:
    """register_all registers HaRepairsCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, HaRepairsCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "ha_repairs"


def test_register_all_registers_ha_persistent_notification() -> None:
    """register_all registers HaPersistentNotificationCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, HaPersistentNotificationCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "ha_persistent_notification"


def test_register_all_registers_ha_anomaly_zscore() -> None:
    """register_all registers HaAnomalyZscoreCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, HaAnomalyZscoreCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "ha_anomaly_zscore"


def test_register_all_registers_ha_safety_sensors() -> None:
    """register_all registers HaSafetySensorsCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, HaSafetySensorsCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "ha_safety_sensors"


def test_register_all_registers_ha_sensor_value() -> None:
    """register_all registers HaSensorValueCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, HaSensorValueCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "ha_sensor_value"


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
