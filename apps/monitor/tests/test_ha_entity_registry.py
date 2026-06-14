"""Tests for the HA entity-registry parse helpers, config, and cache (STAGE-005-037)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast
from unittest.mock import patch

import pytest
import structlog
from structlog.stdlib import BoundLogger

from homelab_monitor.kernel.config import HaRegistryConfig, load_ha_registry_config
from homelab_monitor.kernel.ha.enrichment import (
    RegistryEntry,
    build_registry_index,
    extract_registry,
)
from homelab_monitor.kernel.ha.entity_registry import (
    M_REGISTRY_ENTRIES,
    M_REGISTRY_FETCH_TOTAL,
    M_REGISTRY_LAST_FETCH_TS,
    HaEntityRegistryCache,
    RegistrySnapshot,
)
from homelab_monitor.kernel.ha.errors import HaError
from homelab_monitor.kernel.plugins.io import InMemoryMetricsWriter, MetricEntry

_EXPECTED_REFRESH_SECONDS = 300
_CLAMPED_REFRESH_SECONDS = 60
_MIN_SURVIVED_CALLS = 2


def _log() -> BoundLogger:
    return cast(BoundLogger, structlog.get_logger().bind(component="ha_entity_registry"))


class _FakeWs:
    """One-shot ws-client double: returns a pre-seeded result or HaError."""

    def __init__(self, result: list[object] | dict[str, object] | HaError) -> None:
        self._result = result
        self.calls: list[str] = []

    async def send_command(
        self, type_: str, **fields: object
    ) -> dict[str, object] | list[object] | HaError:
        del fields
        self.calls.append(type_)
        return self._result


class _BlockingWs:
    """ws-client double whose send_command blocks forever (for loop-lifecycle tests)."""

    def __init__(self) -> None:
        self.calls = 0

    async def send_command(
        self, type_: str, **fields: object
    ) -> dict[str, object] | list[object] | HaError:
        del type_, fields
        self.calls += 1
        await asyncio.Event().wait()  # never returns; cancelled by stop_task
        return []  # pragma: no cover -- unreachable


def _entries(writer: InMemoryMetricsWriter, name: str) -> list[MetricEntry]:
    return [e for e in writer.recorded if e.name == name]


# ---------------- extract_registry ----------------


def test_extract_registry_bare_list() -> None:
    payload: list[object] = [{"entity_id": "light.kitchen"}]
    assert extract_registry(payload) == payload


def test_extract_registry_dict_wrapped() -> None:
    inner: list[object] = [{"entity_id": "light.kitchen"}]
    assert extract_registry({"entities": inner}) == inner


def test_extract_registry_garbage_returns_empty() -> None:
    assert extract_registry({}) == []
    assert extract_registry({"entities": "nope"}) == []


# ---------------- build_registry_index ----------------


def test_build_registry_index_reads_all_fields() -> None:
    index = build_registry_index(
        [
            {
                "entity_id": "sensor.cpu",
                "disabled_by": "user",
                "hidden_by": "integration",
                "entity_category": "diagnostic",
            }
        ]
    )
    assert index == {
        "sensor.cpu": RegistryEntry(
            entity_id="sensor.cpu",
            disabled_by="user",
            hidden_by="integration",
            entity_category="diagnostic",
        )
    }


def test_build_registry_index_skips_non_dict_and_empty_id() -> None:
    index = build_registry_index(["not-a-dict", 42, {"entity_id": ""}, {"no_id": 1}])
    assert index == {}


def test_build_registry_index_non_str_fields_become_none() -> None:
    index = build_registry_index(
        [
            {
                "entity_id": "sensor.cpu",
                "disabled_by": None,
                "hidden_by": 123,
                "entity_category": ["x"],
            }
        ]
    )
    entry = index["sensor.cpu"]
    assert entry.disabled_by is None
    assert entry.hidden_by is None
    assert entry.entity_category is None


# ---------------- RegistrySnapshot.is_excluded ----------------


_CFG_ALL = HaRegistryConfig(
    enabled=True,
    exclude_disabled=True,
    exclude_hidden=True,
    exclude_categories=frozenset({"diagnostic"}),
    refresh_seconds=600,
)


def _snap(entry: RegistryEntry) -> RegistrySnapshot:
    return RegistrySnapshot(entries={entry.entity_id: entry}, fetched_at=datetime.now(UTC))


def test_is_excluded_not_populated_is_false() -> None:
    snap = RegistrySnapshot()
    assert snap.is_populated is False
    assert snap.is_excluded("sensor.cpu", _CFG_ALL) is False


def test_is_excluded_unknown_entity_is_false() -> None:
    snap = _snap(RegistryEntry("sensor.cpu", None, None, None))
    assert snap.is_excluded("sensor.other", _CFG_ALL) is False


def test_is_excluded_disabled_respects_toggle() -> None:
    entry = RegistryEntry("sensor.cpu", "user", None, None)
    assert _snap(entry).is_excluded("sensor.cpu", _CFG_ALL) is True
    cfg_off = HaRegistryConfig(exclude_disabled=False)
    assert _snap(entry).is_excluded("sensor.cpu", cfg_off) is False


def test_is_excluded_hidden_respects_toggle() -> None:
    entry = RegistryEntry("sensor.cpu", None, "user", None)
    assert _snap(entry).is_excluded("sensor.cpu", _CFG_ALL) is True
    cfg_off = HaRegistryConfig(exclude_hidden=False)
    assert _snap(entry).is_excluded("sensor.cpu", cfg_off) is False


def test_is_excluded_category_match() -> None:
    entry = RegistryEntry("sensor.cpu", None, None, "DIAGNOSTIC")
    assert _snap(entry).is_excluded("sensor.cpu", _CFG_ALL) is True
    cfg_none = HaRegistryConfig(exclude_categories=frozenset())
    assert _snap(entry).is_excluded("sensor.cpu", cfg_none) is False


# ---------------- HaEntityRegistryCache.refresh ----------------


@pytest.mark.asyncio
async def test_refresh_success_populates_and_emits_metrics() -> None:
    ws = _FakeWs([{"entity_id": "sensor.cpu", "disabled_by": "user"}])
    writer = InMemoryMetricsWriter()
    cache = HaEntityRegistryCache(
        ws_client=ws, config=HaRegistryConfig(), metrics_writer=writer, log=_log()
    )
    await cache.refresh()
    snap = cache.snapshot()
    assert snap.is_populated is True
    assert "sensor.cpu" in snap.entries
    assert ws.calls == ["config/entity_registry/list"]
    ok = _entries(writer, M_REGISTRY_FETCH_TOTAL)
    assert ok and ok[0].labels == {"result": "ok"}
    assert _entries(writer, M_REGISTRY_ENTRIES)[0].value == 1.0
    assert _entries(writer, M_REGISTRY_LAST_FETCH_TS)  # presence only, not exact value


@pytest.mark.asyncio
async def test_refresh_error_keeps_prior_snapshot_and_emits_error_metric() -> None:
    writer = InMemoryMetricsWriter()
    # First a success, then an error: snapshot must survive the error.
    good = _FakeWs([{"entity_id": "sensor.cpu"}])
    cache = HaEntityRegistryCache(
        ws_client=good, config=HaRegistryConfig(), metrics_writer=writer, log=_log()
    )
    await cache.refresh()
    prior = cache.snapshot()
    cache._ws = _FakeWs(HaError(reason="unreachable", message="down"))  # type: ignore[attr-defined]
    await cache.refresh()
    assert cache.snapshot() is prior  # unchanged
    err = [e for e in _entries(writer, M_REGISTRY_FETCH_TOTAL) if e.labels == {"result": "error"}]
    assert len(err) == 1


@pytest.mark.asyncio
async def test_refresh_error_on_empty_cache_stays_not_populated() -> None:
    writer = InMemoryMetricsWriter()
    cache = HaEntityRegistryCache(
        ws_client=_FakeWs(HaError(reason="timeout", message="t")),
        config=HaRegistryConfig(),
        metrics_writer=writer,
        log=_log(),
    )
    await cache.refresh()
    assert cache.snapshot().is_populated is False
    assert _entries(writer, M_REGISTRY_FETCH_TOTAL)[0].labels == {"result": "error"}
    assert _entries(writer, M_REGISTRY_ENTRIES) == []  # no success gauges


# ---------------- start_task / stop_task lifecycle ----------------


@pytest.mark.asyncio
async def test_start_task_idempotent() -> None:
    cache = HaEntityRegistryCache(
        ws_client=_BlockingWs(),
        config=HaRegistryConfig(),
        metrics_writer=InMemoryMetricsWriter(),
        log=_log(),
    )
    cache.start_task()
    first = cache._task  # pyright: ignore[reportPrivateUsage]
    cache.start_task()
    assert cache._task is first  # pyright: ignore[reportPrivateUsage]
    await cache.stop_task()


@pytest.mark.asyncio
async def test_stop_task_cancels_cleanly() -> None:
    cache = HaEntityRegistryCache(
        ws_client=_BlockingWs(),
        config=HaRegistryConfig(),
        metrics_writer=InMemoryMetricsWriter(),
        log=_log(),
    )
    cache.start_task()
    await asyncio.sleep(0)  # let the loop enter refresh + block
    await cache.stop_task()  # must not raise
    assert cache._task is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_stop_task_idempotent_when_never_started() -> None:
    cache = HaEntityRegistryCache(
        ws_client=_BlockingWs(),
        config=HaRegistryConfig(),
        metrics_writer=InMemoryMetricsWriter(),
        log=_log(),
    )
    await cache.stop_task()  # no-op, no raise
    assert cache._task is None  # pyright: ignore[reportPrivateUsage]


async def _yield_once() -> None:
    """Suspend for exactly one event-loop turn (no wall-clock wait)."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[None] = loop.create_future()
    loop.call_soon(fut.set_result, None)
    await fut


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Spin the loop until ``predicate`` holds, bounded by ``timeout`` seconds."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_run_loop_survives_unexpected_refresh_error() -> None:
    """The loop body guard logs + continues if refresh raises unexpectedly.

    Deterministic by the same pattern as test_ha_websocket.py: the loop's
    ``asyncio.sleep`` is patched with a replacement that YIELDS one turn (never a
    no-op, never a real wait), so iteration 1's exception is followed by iteration
    2's ``send_command``. The test then ``_wait_until`` the second call is observed
    under a bounded timeout -- no fixed yield-count race. Iteration 2 blocks on
    ``asyncio.Event().wait()`` so the loop cannot spin runaway.
    """

    class _RaisingWs:
        def __init__(self) -> None:
            self.calls = 0

        async def send_command(self, type_: str, **fields: object) -> list[object]:
            del type_, fields
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
            await asyncio.Event().wait()  # block on 2nd call so loop stays alive
            return []  # pragma: no cover

    async def _yielding_sleep(_delay: float) -> None:
        # Replace the loop's real 60s sleep with a single-turn yield so the loop
        # advances to iteration 2 without waiting on wall-clock time. A no-op
        # (non-yielding) replacement would starve the background task.
        await _yield_once()

    ws = _RaisingWs()
    cfg = HaRegistryConfig(refresh_seconds=60)
    cache = HaEntityRegistryCache(
        ws_client=ws,
        config=cfg,
        metrics_writer=InMemoryMetricsWriter(),
        log=_log(),  # type: ignore[arg-type]
    )
    with patch(
        "homelab_monitor.kernel.ha.entity_registry.asyncio.sleep",
        _yielding_sleep,
    ):
        cache.start_task()
        # Bounded, condition-driven wait: spin until the loop has made its 2nd
        # send_command (proving it survived the 1st iteration's RuntimeError).
        # asyncio.timeout fails loudly if the loop died instead of hanging.
        await _wait_until(lambda: ws.calls >= _MIN_SURVIVED_CALLS)
        await cache.stop_task()
    assert ws.calls >= _MIN_SURVIVED_CALLS  # proves the loop survived the first exception


@pytest.mark.asyncio
async def test_run_loop_retries_on_initial_backoff_until_populated() -> None:
    """Loop retries on short initial backoff after first fetch errors (startup-race fix).

    The _run_loop ``else:`` arm (not-yet-populated path) is exercised: call #1
    returns HaError, so the loop sleeps on the short initial backoff and retries;
    call #2+ returns valid data so the snapshot becomes populated. The patched
    yielding-sleep collapses the initial backoff to a single turn so the test
    completes without real wall-clock delay.
    """

    class _RetryWs:
        def __init__(self) -> None:
            self.calls = 0

        async def send_command(self, type_: str, **fields: object) -> list[object] | HaError:
            del type_, fields
            self.calls += 1
            if self.calls == 1:
                return HaError(reason="unreachable", message="not connected")
            return [{"entity_id": "sensor.cpu", "disabled_by": "user"}]

    async def _yielding_sleep(_delay: float) -> None:
        await _yield_once()

    ws = _RetryWs()
    writer = InMemoryMetricsWriter()
    cache = HaEntityRegistryCache(
        ws_client=ws,
        config=HaRegistryConfig(refresh_seconds=600),
        metrics_writer=writer,
        log=_log(),
    )
    with patch(
        "homelab_monitor.kernel.ha.entity_registry.asyncio.sleep",
        _yielding_sleep,
    ):
        cache.start_task()
        await _wait_until(lambda: cache.snapshot().is_populated)
        await cache.stop_task()

    assert cache.snapshot().is_populated is True
    assert "sensor.cpu" in cache.snapshot().entries
    error_entries = [
        e for e in _entries(writer, M_REGISTRY_FETCH_TOTAL) if e.labels == {"result": "error"}
    ]
    ok_entries = [
        e for e in _entries(writer, M_REGISTRY_FETCH_TOTAL) if e.labels == {"result": "ok"}
    ]
    assert len(error_entries) >= 1, "expected at least one failed fetch (startup race)"
    assert len(ok_entries) >= 1, "expected at least one successful retry"


# ---------------- privacy sentinel ----------------


@pytest.mark.asyncio
async def test_entity_ids_never_emitted_as_metric_labels() -> None:
    """Registry entity_ids must NEVER appear as a self-metric label key or value."""
    ws = _FakeWs(
        [
            {"entity_id": "sensor.secret_thermostat", "disabled_by": "user"},
            {"entity_id": "light.private_bedroom", "hidden_by": "user"},
        ]
    )
    writer = InMemoryMetricsWriter()
    cache = HaEntityRegistryCache(
        ws_client=ws, config=HaRegistryConfig(), metrics_writer=writer, log=_log()
    )
    await cache.refresh()
    for entry in writer.recorded:
        for k, v in entry.labels.items():
            assert "sensor.secret_thermostat" not in k
            assert "sensor.secret_thermostat" not in v
            assert "light.private_bedroom" not in k
            assert "light.private_bedroom" not in v
            assert "entity_id" not in k


# ---------------- load_ha_registry_config ----------------


def test_load_ha_registry_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "HOMELAB_MONITOR_HA_REGISTRY_ENABLED",
        "HOMELAB_MONITOR_HA_REGISTRY_EXCLUDE_DISABLED",
        "HOMELAB_MONITOR_HA_REGISTRY_EXCLUDE_HIDDEN",
        "HOMELAB_MONITOR_HA_REGISTRY_EXCLUDE_CATEGORIES",
        "HOMELAB_MONITOR_HA_REGISTRY_REFRESH_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = load_ha_registry_config()
    assert cfg == HaRegistryConfig()


def test_load_ha_registry_config_parses_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_HA_REGISTRY_ENABLED", "false")
    monkeypatch.setenv("HOMELAB_MONITOR_HA_REGISTRY_EXCLUDE_DISABLED", "no")
    monkeypatch.setenv("HOMELAB_MONITOR_HA_REGISTRY_EXCLUDE_HIDDEN", "0")
    monkeypatch.setenv("HOMELAB_MONITOR_HA_REGISTRY_EXCLUDE_CATEGORIES", " Diagnostic , CONFIG ,")
    monkeypatch.setenv("HOMELAB_MONITOR_HA_REGISTRY_REFRESH_SECONDS", "300")
    cfg = load_ha_registry_config()
    assert cfg.enabled is False
    assert cfg.exclude_disabled is False
    assert cfg.exclude_hidden is False
    assert cfg.exclude_categories == frozenset({"diagnostic", "config"})
    assert cfg.refresh_seconds == _EXPECTED_REFRESH_SECONDS


def test_load_ha_registry_config_clamps_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_HA_REGISTRY_REFRESH_SECONDS", "5")
    assert load_ha_registry_config().refresh_seconds == _CLAMPED_REFRESH_SECONDS


def test_load_ha_registry_config_empty_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_MONITOR_HA_REGISTRY_EXCLUDE_CATEGORIES", "  , ,")
    assert load_ha_registry_config().exclude_categories == frozenset()
