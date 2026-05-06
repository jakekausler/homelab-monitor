"""Tests for kernel/plugins/loader.py — programmatic registry path."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from pydantic import ValidationError

from homelab_monitor.kernel.plugins import (
    LoadedCollector,
    NoopCollector,
    PluginLoader,
)


def test_loader_starts_empty() -> None:
    loader = PluginLoader()
    assert loader.load_all() == []


def test_loader_register_returns_loaded_collector() -> None:
    loader = PluginLoader()
    loaded = loader.register(NoopCollector, {"name": "noop"})
    assert isinstance(loaded, LoadedCollector)
    assert isinstance(loaded.collector, NoopCollector)
    assert loaded.config.name == "noop"


EXPECTED_REGISTRATION_COUNT = 2


def test_loader_register_appends_to_load_all() -> None:
    loader = PluginLoader()
    loaded1 = loader.register(NoopCollector, {"name": "noop_one"})
    loaded2 = loader.register(NoopCollector, {"name": "noop_two"})
    all_loaded = loader.load_all()
    assert len(all_loaded) == EXPECTED_REGISTRATION_COUNT
    assert all_loaded[0] is loaded1
    assert all_loaded[1] is loaded2


def test_loader_register_validates_config_bad_name() -> None:
    loader = PluginLoader()
    with pytest.raises(ValidationError):
        loader.register(NoopCollector, {"name": "X"})


def test_loader_register_validates_config_negative_interval() -> None:
    loader = PluginLoader()
    with pytest.raises(ValidationError):
        loader.register(NoopCollector, {"name": "noop", "interval_seconds": 0})


def test_loader_register_validates_config_extra_field() -> None:
    loader = PluginLoader()
    with pytest.raises(ValidationError):
        loader.register(NoopCollector, {"name": "noop", "garbage": 1})  # type: ignore[typeddict-unknown-key]


def test_loader_register_no_overrides_fails_required_name() -> None:
    loader = PluginLoader()
    with pytest.raises(ValidationError):
        loader.register(NoopCollector)


def test_loader_load_all_returns_defensive_copy() -> None:
    loader = PluginLoader()
    loader.register(NoopCollector, {"name": "noop"})
    loaded = loader.load_all()
    original_count = len(loaded)
    # Mutate the returned list
    loaded.append(loaded[0])
    # load_all() should still return the original count
    assert len(loader.load_all()) == original_count


def test_loaded_collector_is_frozen() -> None:
    loader = PluginLoader()
    loaded = loader.register(NoopCollector, {"name": "noop"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        loaded.collector = NoopCollector()  # type: ignore[misc]


def test_load_subprocess_plugins_skips_non_executable_relative_path(
    tmp_path: Path,
) -> None:
    """load_subprocess_plugins skips plugins with non-executable relative command paths."""

    # Create a temporary manifest directory
    manifest_dir = tmp_path / "plugins"
    manifest_dir.mkdir()

    # Create a manifest with a non-executable relative command
    manifest_path = manifest_dir / "test_plugin.yaml"
    manifest_path.write_text(
        """
name: test-nonexec
interval: 60
command:
  - ./nonexistent_script
  - --arg1
"""
    )

    # Create a loader and load subprocess plugins
    loader = PluginLoader()
    count = loader.load_subprocess_plugins(manifest_dir)

    # Should skip the non-executable plugin
    assert count == 0
    # Verify the plugin was not registered
    assert len(loader.load_all()) == 0


def test_load_subprocess_plugins_skips_command_not_on_path(
    tmp_path: Path,
) -> None:
    """load_subprocess_plugins skips plugins with command not on PATH."""

    # Create a temporary manifest directory
    manifest_dir = tmp_path / "plugins"
    manifest_dir.mkdir()

    # Create a manifest with a non-existent command on PATH
    manifest_path = manifest_dir / "test_plugin.yaml"
    manifest_path.write_text(
        """
name: test-notfound
interval: 60
command:
  - this_command_definitely_does_not_exist_12345
  - --arg1
"""
    )

    # Create a loader and load subprocess plugins
    loader = PluginLoader()
    count = loader.load_subprocess_plugins(manifest_dir)

    # Should skip the plugin with command not on PATH
    assert count == 0
    # Verify the plugin was not registered
    assert len(loader.load_all()) == 0
