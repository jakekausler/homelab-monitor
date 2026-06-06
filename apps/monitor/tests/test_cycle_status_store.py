"""Tests for CycleStatusStore (STAGE-004-027).

All sync; no async needed — the store is synchronous.
"""

from __future__ import annotations

from collections.abc import Callable

from homelab_monitor.kernel.logs.cycle_status import CycleStatusStore
from homelab_monitor.kernel.logs.drain_consumer import DrainCycleResult


def _clock_box() -> tuple[dict[str, int], Callable[[], int]]:
    box: dict[str, int] = {"t": 0}
    return box, (lambda: box["t"])


def _result(*, lines: int = 0) -> DrainCycleResult:
    return DrainCycleResult(
        started_at=1000,
        finished_at=2000,
        lines_processed=lines,
        new_templates=0,
        models_touched=0,
        cycle_status="ok",
        error=None,
    )


def test_begin_then_get_running() -> None:
    store = CycleStatusStore()
    store.begin("c1")
    entry = store.get("c1")
    assert entry is not None
    assert entry.status == "running"
    assert entry.result is None
    assert entry.error is None


def test_complete_sets_done_and_result() -> None:
    store = CycleStatusStore()
    result = _result(lines=5)
    store.begin("c1")
    store.complete("c1", result)
    entry = store.get("c1")
    assert entry is not None
    assert entry.status == "done"
    assert entry.result is result
    assert entry.error is None


def test_fail_sets_failed_and_error() -> None:
    store = CycleStatusStore()
    store.begin("c1")
    store.fail("c1", "boom")
    entry = store.get("c1")
    assert entry is not None
    assert entry.status == "failed"
    assert entry.error == "boom"
    assert entry.result is None


def test_get_unknown_returns_none() -> None:
    store = CycleStatusStore()
    assert store.get("nope") is None


def test_ttl_prune_expires_entry() -> None:
    """Expired entry is pruned and returns None; fresh entry survives."""
    box, clock = _clock_box()
    ttl_ms = 100
    store = CycleStatusStore(ttl_ms=ttl_ms, clock=clock)

    # c1 at t=0; advance past c1's TTL
    box["t"] = 0
    store.begin("c1")
    box["t"] = 101  # c1 is 101ms old → > 100ms TTL → expires
    result = store.get("c1")
    assert result is None  # expired and pruned

    # Fresh scenario: c1 begins at t=0, c2 begins at t=60.
    # At t=120: c1 is 120ms old (>100ms → expires), c2 is 60ms old (<=100ms → alive).
    box2, clock2 = _clock_box()
    store2 = CycleStatusStore(ttl_ms=ttl_ms, clock=clock2)
    box2["t"] = 0
    store2.begin("c1")
    box2["t"] = 60
    store2.begin("c2")
    box2["t"] = 120  # c1 expires; c2 survives (120-60=60 which is NOT > 100)
    r_c1 = store2.get("c1")
    assert r_c1 is None  # expired
    r_c2 = store2.get("c2")
    assert r_c2 is not None  # still alive (only 60ms old)


def test_complete_preserves_created_ms() -> None:
    """TTL is measured from begin(), not from complete()."""
    box, clock = _clock_box()
    ttl_ms = 100
    store = CycleStatusStore(ttl_ms=ttl_ms, clock=clock)

    box["t"] = 0
    store.begin("c1")
    box["t"] = 50  # advance; cycle completes before TTL
    result = _result()
    store.complete("c1", result)

    # At t=101, the entry should be expired (>100ms from begin at t=0)
    box["t"] = 101
    entry = store.get("c1")
    assert entry is None  # TTL expired from begin, not from complete


def test_complete_without_begin_uses_clock() -> None:
    """complete() without a prior begin() still records an entry using the clock."""
    box, clock = _clock_box()
    box["t"] = 42
    store = CycleStatusStore(clock=clock)
    result = _result()
    store.complete("orphan", result)
    entry = store.get("orphan")
    assert entry is not None
    assert entry.status == "done"
    assert entry.created_ms == 42  # noqa: PLR2004
    assert entry.result is result


def test_fail_without_begin_uses_clock() -> None:
    """fail() without a prior begin() still records an entry using the clock."""
    box, clock = _clock_box()
    box["t"] = 99
    store = CycleStatusStore(clock=clock)
    store.fail("orphan", "some error")
    entry = store.get("orphan")
    assert entry is not None
    assert entry.status == "failed"
    assert entry.created_ms == 99  # noqa: PLR2004
    assert entry.error == "some error"
