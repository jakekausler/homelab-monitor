"""Tests for SyncSecretsResolver: frozen, IPC-safe, filterable."""

from __future__ import annotations

import dataclasses
import pickle

import pytest

from homelab_monitor.kernel.secrets.resolver import SyncSecretsResolver


def test_get_known_returns_value() -> None:
    r = SyncSecretsResolver(_values={"alpha": "a", "beta": "b"})  # pyright: ignore[reportPrivateUsage]
    assert r.get("alpha") == "a"


def test_get_unknown_returns_none() -> None:
    r = SyncSecretsResolver(_values={"alpha": "a"})  # pyright: ignore[reportPrivateUsage]
    assert r.get("zeta") is None


def test_list_names_sorted() -> None:
    r = SyncSecretsResolver(_values={"beta": "b", "alpha": "a", "gamma": "g"})  # pyright: ignore[reportPrivateUsage]
    assert r.list_names() == ["alpha", "beta", "gamma"]


def test_default_values_is_empty() -> None:
    r = SyncSecretsResolver()
    assert r.list_names() == []


def test_as_mapping_is_read_only() -> None:
    r = SyncSecretsResolver(_values={"alpha": "a"})  # pyright: ignore[reportPrivateUsage]
    m = r.as_mapping()
    assert m["alpha"] == "a"
    with pytest.raises(TypeError):
        m["alpha"] = "tampered"  # type: ignore[index]


def test_frozen_dataclass_cannot_reassign_fields() -> None:
    """The dataclass is frozen — reassigning a field raises FrozenInstanceError."""
    r = SyncSecretsResolver(_values={"alpha": "a"})  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(dataclasses.FrozenInstanceError):
        r._values = {}  # pyright: ignore[reportPrivateUsage,reportAttributeAccessIssue]


def test_pickle_round_trip() -> None:
    """SyncSecretsResolver is IPC-serializable (used across subprocess boundary)."""
    r = SyncSecretsResolver(_values={"alpha": "a", "beta": "b"})  # pyright: ignore[reportPrivateUsage]
    pickled = pickle.dumps(r)
    unpickled = pickle.loads(pickled)  # type: ignore[no-untyped-call]
    assert unpickled.get("alpha") == "a"
    assert unpickled.list_names() == ["alpha", "beta"]


def test_filtered_returns_subset() -> None:
    """Filtering by declared names yields only matching entries."""
    r = SyncSecretsResolver(_values={"alpha": "a", "beta": "b", "gamma": "g"})  # pyright: ignore[reportPrivateUsage]
    sub = r.filtered(["alpha", "gamma", "missing"])
    assert sub.list_names() == ["alpha", "gamma"]
    assert sub.get("alpha") == "a"
    assert sub.get("gamma") == "g"
    assert sub.get("missing") is None
    # Original is untouched.
    assert r.list_names() == ["alpha", "beta", "gamma"]


def test_filtered_returns_independent_instance() -> None:
    """The filtered resolver is a new SyncSecretsResolver, not a shared view."""
    r = SyncSecretsResolver(_values={"alpha": "a"})  # pyright: ignore[reportPrivateUsage]
    sub = r.filtered(["alpha"])
    assert isinstance(sub, SyncSecretsResolver)
    assert sub is not r
