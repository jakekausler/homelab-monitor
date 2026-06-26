"""Tests for the Synology integration bundle register_all (STAGE-008-002)."""

from __future__ import annotations

import pytest

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.loader import LoadedCollector, PluginLoader
from homelab_monitor.plugins.collectors.integrations.synology import register_all
from homelab_monitor.plugins.collectors.integrations.synology.backup import (
    SynologyBackupCollector,
)
from homelab_monitor.plugins.collectors.integrations.synology.cameras import (
    SynologyCameraCollector,
)
from homelab_monitor.plugins.collectors.integrations.synology.pool import (
    SynologyPoolCollector,
)
from homelab_monitor.plugins.collectors.integrations.synology.replication import (
    SynologyReplicationCollector,
)
from homelab_monitor.plugins.collectors.integrations.synology.security import (
    SynologySecurityCollector,
)
from homelab_monitor.plugins.collectors.integrations.synology.storage import (
    SynologyStorageCollector,
)
from homelab_monitor.plugins.collectors.integrations.synology.system import (
    SynologySystemCollector,
)
from homelab_monitor.plugins.collectors.integrations.synology.updates import (
    SynologyUpdatesCollector,
)
from homelab_monitor.plugins.collectors.integrations.synology.ups import (
    SynologyUPSCollector,
)
from homelab_monitor.plugins.collectors.integrations.synology.utilization import (
    SynologyUtilizationCollector,
)

_EXPECTED_INTERVAL = 300
_EXPECTED_TIMEOUT = 30
_EXPECTED_SYSTEM_INTERVAL = 60
_EXPECTED_UTILIZATION_INTERVAL = 60
_EXPECTED_UPS_INTERVAL = 60
_EXPECTED_BACKUP_INTERVAL = 300
_EXPECTED_REPLICATION_INTERVAL = 300
_EXPECTED_UPDATES_INTERVAL = 3600
_EXPECTED_SECURITY_INTERVAL = 3600
_EXPECTED_CAMERA_INTERVAL = 60


def test_register_all_registers_storage() -> None:
    """register_all registers SynologyStorageCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, SynologyStorageCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "synology_storage"
    assert record.config.interval_seconds == _EXPECTED_INTERVAL
    assert record.config.timeout_seconds == _EXPECTED_TIMEOUT


def test_register_all_registers_pool() -> None:
    """register_all registers SynologyPoolCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, SynologyPoolCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "synology_pool"
    assert record.config.interval_seconds == _EXPECTED_INTERVAL
    assert record.config.timeout_seconds == _EXPECTED_TIMEOUT


def test_register_all_registers_system() -> None:
    """register_all registers SynologySystemCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, SynologySystemCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "synology_system"
    assert record.config.interval_seconds == _EXPECTED_SYSTEM_INTERVAL
    assert record.config.timeout_seconds == _EXPECTED_TIMEOUT


def test_register_all_registers_utilization() -> None:
    """register_all registers SynologyUtilizationCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, SynologyUtilizationCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "synology_utilization"
    assert record.config.interval_seconds == _EXPECTED_UTILIZATION_INTERVAL
    assert record.config.timeout_seconds == _EXPECTED_TIMEOUT


def test_register_all_registers_ups() -> None:
    """register_all registers SynologyUPSCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, SynologyUPSCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "synology_ups"
    assert record.config.interval_seconds == _EXPECTED_UPS_INTERVAL
    assert record.config.timeout_seconds == _EXPECTED_TIMEOUT


def test_register_all_registers_backup() -> None:
    """register_all registers SynologyBackupCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, SynologyBackupCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "synology_backup"
    assert record.config.interval_seconds == _EXPECTED_BACKUP_INTERVAL
    assert record.config.timeout_seconds == _EXPECTED_TIMEOUT


def test_register_all_registers_replication() -> None:
    """register_all registers SynologyReplicationCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, SynologyReplicationCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "synology_replication"
    assert record.config.interval_seconds == _EXPECTED_REPLICATION_INTERVAL
    assert record.config.timeout_seconds == _EXPECTED_TIMEOUT


def test_register_all_registers_updates() -> None:
    """register_all registers SynologyUpdatesCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, SynologyUpdatesCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "synology_updates"
    assert record.config.interval_seconds == _EXPECTED_UPDATES_INTERVAL
    assert record.config.timeout_seconds == _EXPECTED_TIMEOUT


def test_register_all_registers_security() -> None:
    """register_all registers SynologySecurityCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, SynologySecurityCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "synology_security"
    assert record.config.interval_seconds == _EXPECTED_SECURITY_INTERVAL
    assert record.config.timeout_seconds == _EXPECTED_TIMEOUT


def test_register_all_registers_cameras() -> None:
    """register_all registers SynologyCameraCollector with the derived config."""
    loader = PluginLoader()
    register_all(loader)
    loaded = loader.load_all()
    records = [r for r in loaded if isinstance(r.collector, SynologyCameraCollector)]
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, LoadedCollector)
    assert record.config.name == "synology_cameras"
    assert record.config.interval_seconds == _EXPECTED_CAMERA_INTERVAL
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
