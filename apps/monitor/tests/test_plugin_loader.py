"""Tests for kernel/plugins/loader.py — programmatic registry path."""

from __future__ import annotations

import dataclasses

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
