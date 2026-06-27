"""STAGE-008-032: per-collector YAML override loading + subclass-aware register().

Covers every branch of ``load_collector_overrides`` and of the YAML-merge + subclass
validation path in ``PluginLoader.register``. Env-injected via
``HOMELAB_MONITOR_COLLECTOR_OVERRIDES_DIR`` -> tmp_path (mirrors test_config.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import Field, ValidationError

from homelab_monitor.kernel.plugins.base import BaseCollector
from homelab_monitor.kernel.plugins.collector_config import load_collector_overrides
from homelab_monitor.kernel.plugins.context import CollectorContext
from homelab_monitor.kernel.plugins.loader import PluginLoader
from homelab_monitor.kernel.plugins.types import CollectorConfig, CollectorResult, RunKind
from homelab_monitor.plugins.collectors.builtin.host import (
    HostCollector,
    HostCollectorConfig,
)
from homelab_monitor.plugins.collectors.builtin.synology_mount_health import (
    SynologyMountHealthCollector,
    SynologyMountHealthCollectorConfig,
)
from homelab_monitor.plugins.collectors.builtin.watched_dir_size import (
    WatchedDirSizeCollector,
    WatchedDirSizeCollectorConfig,
)

_ENV = "HOMELAB_MONITOR_COLLECTOR_OVERRIDES_DIR"
_MOVIES = "/rackstation/Movies"
_DEFAULT_HOST_MOUNT = "/rackstation"


# --- test collectors -----------------------------------------------------------------------


class _ExtraConfig(CollectorConfig):
    """Subclass config exposing one extra field, used to test subclass validation."""

    model_config = CollectorConfig.model_config
    widget_paths: list[str] = Field(default_factory=list)


class _SubclassCollector(BaseCollector):
    """Concrete collector declaring a config subclass."""

    name: ClassVar[str] = "subclass-collector"
    interval: ClassVar = HostCollector.interval
    timeout: ClassVar = HostCollector.timeout
    run_kind: ClassVar[RunKind] = RunKind.ASYNC
    config_class: ClassVar[type[CollectorConfig]] = _ExtraConfig

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        return CollectorResult(ok=True)


class _BaseConfigCollector(BaseCollector):
    """Concrete collector with NO config_class override (uses base CollectorConfig)."""

    name: ClassVar[str] = "base-config-collector"
    interval: ClassVar = HostCollector.interval
    timeout: ClassVar = HostCollector.timeout
    run_kind: ClassVar[RunKind] = RunKind.ASYNC

    async def run(self, ctx: CollectorContext) -> CollectorResult:
        return CollectorResult(ok=True)


def _register_dict(name: str) -> dict[str, object]:
    return {"name": name, "interval_seconds": 60, "timeout_seconds": 30}


# --- direct unit tests of load_collector_overrides -----------------------------------------


def test_overrides_absent_dir_returns_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pointing the env at a non-existent dir => {} (file is not is_file())."""
    monkeypatch.setenv(_ENV, str(tmp_path / "does-not-exist"))
    assert load_collector_overrides("anything") == {}


def test_overrides_absent_file_returns_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Dir exists but no <name>.yaml => {}."""
    monkeypatch.setenv(_ENV, str(tmp_path))
    assert load_collector_overrides("synology_mount_health") == {}


def test_overrides_present_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A valid mapping file is parsed and returned."""
    (tmp_path / "synology_mount_health.yaml").write_text(f'synology_mounts: ["{_MOVIES}"]\n')
    monkeypatch.setenv(_ENV, str(tmp_path))
    assert load_collector_overrides("synology_mount_health") == {"synology_mounts": [_MOVIES]}


def test_overrides_present_empty_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An empty YAML file (safe_load -> None) => {} via the `or {}` branch."""
    (tmp_path / "host.yaml").write_text("")
    monkeypatch.setenv(_ENV, str(tmp_path))
    assert load_collector_overrides("host") == {}


def test_overrides_present_non_mapping_root_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A YAML list root raises ValueError (mirrors config.py's isinstance guard)."""
    (tmp_path / "host.yaml").write_text("- a\n- b\n")
    monkeypatch.setenv(_ENV, str(tmp_path))
    with pytest.raises(ValueError, match="collector override root must be a mapping"):
        load_collector_overrides("host")


# --- register(): subclass-aware + YAML-merge -----------------------------------------------


def test_register_no_yaml_uses_subclass_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No yaml file => merged == overrides => subclass validates with Field defaults."""
    monkeypatch.setenv(_ENV, str(tmp_path))
    loader = PluginLoader()
    loaded = loader.register(SynologyMountHealthCollector, _register_dict("synology_mount_health"))
    assert isinstance(loaded.config, SynologyMountHealthCollectorConfig)
    assert loaded.config.synology_mounts == []


def test_register_host_no_yaml_default_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Host collector with no yaml keeps the baked extra_mountpoints default."""
    monkeypatch.setenv(_ENV, str(tmp_path))
    loader = PluginLoader()
    loaded = loader.register(HostCollector, _register_dict("host"))
    assert isinstance(loaded.config, HostCollectorConfig)
    assert loaded.config.extra_mountpoints == [_DEFAULT_HOST_MOUNT]


def test_register_yaml_value_flows_to_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A present valid yaml file flows into the registered collector's config."""
    (tmp_path / "synology_mount_health.yaml").write_text(f'synology_mounts: ["{_MOVIES}"]\n')
    monkeypatch.setenv(_ENV, str(tmp_path))
    loader = PluginLoader()
    loaded = loader.register(SynologyMountHealthCollector, _register_dict("synology_mount_health"))
    assert isinstance(loaded.config, SynologyMountHealthCollectorConfig)
    assert loaded.config.synology_mounts == [_MOVIES]


def test_register_yaml_wins_over_register_dict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a key collision YAML wins (operator override beats baked default)."""
    (tmp_path / "host.yaml").write_text("interval_seconds: 999\n")
    monkeypatch.setenv(_ENV, str(tmp_path))
    loader = PluginLoader()
    loaded = loader.register(HostCollector, _register_dict("host"))
    expected_interval = 999
    assert loaded.config.interval_seconds == expected_interval


def test_register_malformed_yaml_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-mapping yaml root surfaces as ValueError out of register()."""
    (tmp_path / "host.yaml").write_text("- a\n- b\n")
    monkeypatch.setenv(_ENV, str(tmp_path))
    loader = PluginLoader()
    with pytest.raises(ValueError, match="collector override root must be a mapping"):
        loader.register(HostCollector, _register_dict("host"))


def test_register_unknown_yaml_key_raises_validation_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unknown key for a subclass-configured collector => ValidationError (extra=forbid)."""
    (tmp_path / "host.yaml").write_text("not_a_real_field: 1\n")
    monkeypatch.setenv(_ENV, str(tmp_path))
    loader = PluginLoader()
    with pytest.raises(ValidationError):
        loader.register(HostCollector, _register_dict("host"))


def test_register_base_collector_no_yaml_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A base-config collector with no yaml validates against base CollectorConfig."""
    monkeypatch.setenv(_ENV, str(tmp_path))
    loader = PluginLoader()
    loaded = loader.register(_BaseConfigCollector, _register_dict("base-config-collector"))
    assert type(loaded.config) is CollectorConfig
    assert loaded.config.name == "base-config-collector"


def test_register_base_collector_unknown_yaml_key_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A base-config collector + yaml with an unknown key => ValidationError."""
    (tmp_path / "base-config-collector.yaml").write_text("mystery: 1\n")
    monkeypatch.setenv(_ENV, str(tmp_path))
    loader = PluginLoader()
    with pytest.raises(ValidationError):
        loader.register(_BaseConfigCollector, _register_dict("base-config-collector"))


def test_register_subclass_extra_field_validates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A collector WITH a config_class subclass: the extra field validates from yaml."""
    (tmp_path / "subclass-collector.yaml").write_text('widget_paths: ["/a", "/b"]\n')
    monkeypatch.setenv(_ENV, str(tmp_path))
    loader = PluginLoader()
    loaded = loader.register(_SubclassCollector, _register_dict("subclass-collector"))
    assert isinstance(loaded.config, _ExtraConfig)
    assert loaded.config.widget_paths == ["/a", "/b"]


def test_register_non_str_name_uses_str_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-str name in the register dict exercises the str() fallback branch.

    The path lookup uses str(123)='123'; no 123.yaml exists so overrides stay {};
    model_validate then raises on the name pattern (str needed) -> ValidationError.
    """
    monkeypatch.setenv(_ENV, str(tmp_path))
    loader = PluginLoader()
    with pytest.raises(ValidationError):
        loader.register(_BaseConfigCollector, {"name": 123})


def test_register_none_overrides_falsy_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """config_overrides=None exercises the `else {}` branch (then fails name validation)."""
    monkeypatch.setenv(_ENV, str(tmp_path))
    loader = PluginLoader()
    with pytest.raises(ValidationError):
        loader.register(_BaseConfigCollector, None)


# --- wiring assertions ---------------------------------------------------------------------


def test_host_collector_config_class_wired() -> None:
    """HostCollector declares HostCollectorConfig."""
    assert HostCollector.config_class is HostCollectorConfig


def test_synology_collector_config_class_wired() -> None:
    """SynologyMountHealthCollector declares its subclass."""
    assert SynologyMountHealthCollector.config_class is SynologyMountHealthCollectorConfig


def test_watched_dir_size_collector_config_class_wired() -> None:
    """WatchedDirSizeCollector declares its subclass."""
    assert WatchedDirSizeCollector.config_class is WatchedDirSizeCollectorConfig


def test_base_collector_default_config_class() -> None:
    """BaseCollector's default config_class is the plain base CollectorConfig."""
    assert BaseCollector.config_class is CollectorConfig


def test_register_watched_dir_nested_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """WatchedDirSizeCollector accepts YAML list of nested WatchedDirectory mappings."""
    (tmp_path / "watched_dir_size.yaml").write_text(
        "watched_directories:\n  - path: /tmp\n    warn_bytes: 1073741824\n"
        "    crit_bytes: 4294967296\n"
    )
    monkeypatch.setenv(_ENV, str(tmp_path))
    loader = PluginLoader()
    loaded = loader.register(WatchedDirSizeCollector, _register_dict("watched_dir_size"))
    assert isinstance(loaded.config, WatchedDirSizeCollectorConfig)
    assert len(loaded.config.watched_directories) == 1
