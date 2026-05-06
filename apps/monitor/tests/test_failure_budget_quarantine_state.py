"""Tests for FailureBudget.quarantine_state() — coverage for failure_budget.py:148."""

from __future__ import annotations

from unittest.mock import MagicMock

from homelab_monitor.kernel.scheduler.failure_budget import (
    FailureBudget,
    QuarantineState,
)


def test_quarantine_state_returns_none_for_non_quarantined_collector() -> None:
    """quarantine_state(name) returns None when collector is not quarantined."""
    repo = MagicMock()
    log = MagicMock()
    budget = FailureBudget(repo, log)

    result = budget.quarantine_state("healthy_collector")
    assert result is None


def test_quarantine_state_returns_state_when_quarantined() -> None:
    """quarantine_state(name) returns QuarantineState for quarantined collector."""
    repo = MagicMock()
    log = MagicMock()
    budget = FailureBudget(repo, log)

    # Manually set quarantine state (simulating loaded state)
    consecutive_failures = 5
    q_state = QuarantineState(
        consecutive_failures=consecutive_failures,
        quarantined_at="2026-05-05T10:00:00Z",
        quarantine_reason="exceeded_failure_budget",
    )
    budget._quarantined["bad_collector"] = q_state  # pyright: ignore[reportPrivateUsage]

    result = budget.quarantine_state("bad_collector")
    assert result is not None
    assert result.consecutive_failures == consecutive_failures
    assert result.quarantined_at == "2026-05-05T10:00:00Z"
    assert result.quarantine_reason == "exceeded_failure_budget"


def test_quarantine_state_multiple_collectors() -> None:
    """quarantine_state distinguishes between multiple collectors."""
    repo = MagicMock()
    log = MagicMock()
    budget = FailureBudget(repo, log)

    # Add two different quarantine states
    failures_1 = 3
    failures_2 = 7
    q_state_1 = QuarantineState(
        consecutive_failures=failures_1,
        quarantined_at="2026-05-05T10:00:00Z",
        quarantine_reason="reason1",
    )
    q_state_2 = QuarantineState(
        consecutive_failures=failures_2,
        quarantined_at="2026-05-05T11:00:00Z",
        quarantine_reason="reason2",
    )
    budget._quarantined["collector1"] = q_state_1  # pyright: ignore[reportPrivateUsage]
    budget._quarantined["collector2"] = q_state_2  # pyright: ignore[reportPrivateUsage]

    result1 = budget.quarantine_state("collector1")
    result2 = budget.quarantine_state("collector2")
    result3 = budget.quarantine_state("collector3")

    assert result1 is not None and result1.consecutive_failures == failures_1
    assert result2 is not None and result2.consecutive_failures == failures_2
    assert result3 is None


def test_is_quarantined_matches_quarantine_state() -> None:
    """is_quarantined and quarantine_state are consistent."""
    repo = MagicMock()
    log = MagicMock()
    budget = FailureBudget(repo, log)

    q_state = QuarantineState(
        consecutive_failures=5,
        quarantined_at="2026-05-05T10:00:00Z",
        quarantine_reason="test",
    )
    budget._quarantined["test_collector"] = q_state  # pyright: ignore[reportPrivateUsage]

    # Both should agree
    assert budget.is_quarantined("test_collector") is True
    assert budget.quarantine_state("test_collector") is not None
    assert budget.is_quarantined("other_collector") is False
    assert budget.quarantine_state("other_collector") is None
