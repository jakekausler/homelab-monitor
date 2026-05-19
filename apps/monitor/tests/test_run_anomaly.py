"""Unit tests for kernel/cron/run_anomaly.py (STAGE-002-014).

Project test conventions:
- @pytest.mark.asyncio for async tests (pure-python tests here are sync)
- noqa: PLR2004 for magic number assertions
- noqa: PLC0415 for function-scoped imports
- pyright: ignore[reportPrivateUsage] for private symbol access
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from homelab_monitor.kernel.config import CronAnomalyConfig
from homelab_monitor.kernel.cron.run_anomaly import (
    _dominant_exit_code,  # pyright: ignore[reportPrivateUsage]
    _median,  # pyright: ignore[reportPrivateUsage]
    _p95,  # pyright: ignore[reportPrivateUsage]
    _rule_duration_outlier,  # pyright: ignore[reportPrivateUsage]
    _rule_output_size_spike,  # pyright: ignore[reportPrivateUsage]
    evaluate_run,
)
from homelab_monitor.kernel.cron.run_repository import CronRunRecord

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_DEFAULT_CFG = CronAnomalyConfig(
    min_history=5,
    rolling_window=20,
    duration_k=4.0,
    output_band=0.5,
)


def _make_run(  # noqa: PLR0913
    *,
    run_id: str = "run-0",
    cron_fingerprint: str = "fp1",
    state: str = "ok",
    duration_seconds: float | None = 10.0,
    exit_code: int | None = 0,
    line_count: int | None = 100,
    byte_count: int | None = 5000,
    source: str = "wrapper",
) -> CronRunRecord:
    return CronRunRecord(
        run_id=run_id,
        cron_fingerprint=cron_fingerprint,
        source=source,
        state=state,
        started_at="2026-05-19T00:00:00+00:00",
        ended_at="2026-05-19T00:00:10+00:00",
        duration_seconds=duration_seconds,
        exit_code=exit_code,
        vl_window_start=None,
        vl_window_end=None,
        overlapping=False,
        enriched_at="2026-05-19T00:00:20+00:00",
        line_count=line_count,
        byte_count=byte_count,
        content_digest=None,
        anomaly_flags="",
    )


def _make_history(
    count: int,
    *,
    state: str = "ok",
    duration_seconds: float = 10.0,
    exit_code: int | None = 0,
    line_count: int | None = 100,
) -> list[CronRunRecord]:
    return [
        _make_run(
            run_id=f"hist-{i}",
            state=state,
            duration_seconds=duration_seconds,
            exit_code=exit_code,
            line_count=line_count,
        )
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# Helper unit tests: _p95
# ---------------------------------------------------------------------------


def test_p95_empty_returns_none() -> None:
    assert _p95([]) is None


def test_p95_single_element_returns_that_value() -> None:
    assert _p95([42.0]) == 42.0  # noqa: PLR2004


def test_p95_small_sample() -> None:
    # For a small sorted sequence the p95 cut should be >= median
    values = [float(i) for i in range(1, 11)]
    result = _p95(values)
    assert result is not None
    assert result >= 5.0  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Helper unit tests: _median
# ---------------------------------------------------------------------------


def test_median_empty_returns_none() -> None:
    assert _median([]) is None


def test_median_odd_length() -> None:
    assert _median([1.0, 2.0, 3.0]) == 2.0  # noqa: PLR2004


def test_median_even_length() -> None:
    assert _median([1.0, 2.0, 3.0, 4.0]) == 2.5  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Helper unit tests: _dominant_exit_code
# ---------------------------------------------------------------------------


def test_dominant_exit_code_all_none_returns_none() -> None:
    history = [_make_run(exit_code=None) for _ in range(5)]
    assert _dominant_exit_code(history) is None


def test_dominant_exit_code_no_tie() -> None:
    history = [_make_run(exit_code=0)] * 3 + [_make_run(exit_code=1)]
    assert _dominant_exit_code(history) == 0


def test_dominant_exit_code_tie_broken_by_lower_code() -> None:
    # Two each of code 1 and code 2 — tie → lower wins (1)
    history = [_make_run(exit_code=1), _make_run(exit_code=2)] * 2
    assert _dominant_exit_code(history) == 1


# ---------------------------------------------------------------------------
# min_history gate
# ---------------------------------------------------------------------------


def test_min_history_gate_returns_empty_string() -> None:
    """History shorter than min_history → empty string regardless of anomalous run."""
    cfg = CronAnomalyConfig(min_history=10, rolling_window=20, duration_k=4.0, output_band=0.5)
    run = _make_run(duration_seconds=999_999.0)  # extremely long
    history = _make_history(9, duration_seconds=1.0)  # only 9 < min_history=10
    result = evaluate_run(run, history, cfg)
    assert result == ""


def test_min_history_gate_passes_at_exact_threshold() -> None:
    """History of exactly min_history entries passes the gate."""
    cfg = CronAnomalyConfig(min_history=5, rolling_window=20, duration_k=4.0, output_band=0.5)
    # Normal run against normal history → no flags but gate passes (empty still)
    run = _make_run(duration_seconds=10.0)
    history = _make_history(5, duration_seconds=10.0)
    result = evaluate_run(run, history, cfg)
    assert result == ""  # no anomaly in normal data, but gate passed


# ---------------------------------------------------------------------------
# Rule: duration_outlier — fires
# ---------------------------------------------------------------------------


def test_duration_outlier_fires_when_run_far_exceeds_p95() -> None:
    """duration_seconds >> k * p95 → duration_outlier flag."""
    cfg = _DEFAULT_CFG  # k=4.0
    run = _make_run(duration_seconds=1000.0)  # 100x normal
    history = _make_history(10, duration_seconds=10.0)
    assert evaluate_run(run, history, cfg) == "duration_outlier"


# ---------------------------------------------------------------------------
# Rule: duration_outlier — no-fire
# ---------------------------------------------------------------------------


def test_duration_outlier_does_not_fire_on_normal_duration() -> None:
    run = _make_run(duration_seconds=11.0)  # just slightly above median
    history = _make_history(10, duration_seconds=10.0)
    assert "duration_outlier" not in evaluate_run(run, history, _DEFAULT_CFG)


def test_duration_outlier_skipped_when_duration_is_none() -> None:
    run = _make_run(duration_seconds=None)
    history = _make_history(10, duration_seconds=10.0)
    assert "duration_outlier" not in evaluate_run(run, history, _DEFAULT_CFG)


def test_duration_outlier_skipped_when_history_durations_all_none() -> None:
    run = _make_run(duration_seconds=999.0)
    history = _make_history(10, duration_seconds=10.0)
    # Replace all history durations with None
    history_no_dur = [replace(r, duration_seconds=None) for r in history]
    assert "duration_outlier" not in evaluate_run(run, history_no_dur, _DEFAULT_CFG)


# ---------------------------------------------------------------------------
# Rule: exit_code_changed — fires
# ---------------------------------------------------------------------------


def test_exit_code_changed_fires_when_code_differs_from_dominant() -> None:
    # Use state="ok" so new_failure does NOT fire — isolates exit_code_changed
    run = _make_run(state="ok", exit_code=1)
    history = _make_history(10, exit_code=0)
    assert evaluate_run(run, history, _DEFAULT_CFG) == "exit_code_changed"


# ---------------------------------------------------------------------------
# Rule: exit_code_changed — no-fire
# ---------------------------------------------------------------------------


def test_exit_code_changed_does_not_fire_when_code_matches_dominant() -> None:
    run = _make_run(exit_code=0)
    history = _make_history(10, exit_code=0)
    assert "exit_code_changed" not in evaluate_run(run, history, _DEFAULT_CFG)


def test_exit_code_changed_skipped_when_run_exit_code_is_none() -> None:
    run = _make_run(exit_code=None)
    history = _make_history(10, exit_code=0)
    assert "exit_code_changed" not in evaluate_run(run, history, _DEFAULT_CFG)


def test_exit_code_changed_skipped_when_history_all_none() -> None:
    run = _make_run(exit_code=1)
    history = [replace(r, exit_code=None) for r in _make_history(10, exit_code=0)]
    assert "exit_code_changed" not in evaluate_run(run, history, _DEFAULT_CFG)


# ---------------------------------------------------------------------------
# Rule: output_size_spike — fires
# ---------------------------------------------------------------------------


def test_output_size_spike_fires_when_line_count_far_above_median() -> None:
    """line_count 3x median (band=0.5 → threshold=1.5x) → spike."""
    run = _make_run(line_count=300)
    history = _make_history(10, line_count=100)
    assert evaluate_run(run, history, _DEFAULT_CFG) == "output_size_spike"


# ---------------------------------------------------------------------------
# Rule: output_size_spike — no-fire
# ---------------------------------------------------------------------------


def test_output_size_spike_does_not_fire_on_normal_output() -> None:
    run = _make_run(line_count=110)  # within 50% band
    history = _make_history(10, line_count=100)
    assert "output_size_spike" not in evaluate_run(run, history, _DEFAULT_CFG)


def test_output_size_spike_skipped_when_line_count_is_none() -> None:
    run = _make_run(line_count=None)
    history = _make_history(10, line_count=100)
    assert "output_size_spike" not in evaluate_run(run, history, _DEFAULT_CFG)


def test_output_size_spike_skipped_when_history_line_counts_all_none() -> None:
    run = _make_run(line_count=9999)
    history = [replace(r, line_count=None) for r in _make_history(10, line_count=100)]
    assert "output_size_spike" not in evaluate_run(run, history, _DEFAULT_CFG)


def test_output_size_spike_fires_when_history_all_silent_and_run_has_output() -> None:
    """History with all line_count=0 and run with line_count>0 → spike."""
    run = _make_run(line_count=100)
    history = _make_history(10, line_count=0)
    assert evaluate_run(run, history, _DEFAULT_CFG) == "output_size_spike"


# ---------------------------------------------------------------------------
# Rule: output_size_drop — fires
# ---------------------------------------------------------------------------


def test_output_size_drop_fires_when_line_count_far_below_median() -> None:
    """line_count 10 vs median 100 (band=0.5 → threshold=50) → drop."""
    run = _make_run(line_count=10)
    history = _make_history(10, line_count=100)
    # Also make sure unexpected_empty doesn't fire (line_count != 0)
    flags = evaluate_run(run, history, _DEFAULT_CFG)
    assert "output_size_drop" in flags


# ---------------------------------------------------------------------------
# Rule: output_size_drop — no-fire
# ---------------------------------------------------------------------------


def test_output_size_drop_does_not_fire_on_normal_output() -> None:
    run = _make_run(line_count=80)  # within 50% band of 100
    history = _make_history(10, line_count=100)
    assert "output_size_drop" not in evaluate_run(run, history, _DEFAULT_CFG)


def test_output_size_drop_skipped_when_median_is_zero() -> None:
    """History median=0 → drop rule is skipped (silent cron baseline)."""
    run = _make_run(line_count=0)
    history = _make_history(10, line_count=0)
    flags = evaluate_run(run, history, _DEFAULT_CFG)
    assert "output_size_drop" not in flags


# ---------------------------------------------------------------------------
# Rule: unexpected_empty — fires
# ---------------------------------------------------------------------------


def test_unexpected_empty_fires_when_run_is_silent_but_history_mostly_nonempty() -> None:
    run = _make_run(line_count=0)
    history = _make_history(10, line_count=100)  # all non-empty
    flags = evaluate_run(run, history, _DEFAULT_CFG)
    assert "unexpected_empty" in flags


# ---------------------------------------------------------------------------
# Rule: unexpected_empty — no-fire
# ---------------------------------------------------------------------------


def test_unexpected_empty_does_not_fire_when_history_mostly_silent() -> None:
    run = _make_run(line_count=0)
    # 6 silent, 4 non-empty → majority silent → no flag
    history = _make_history(6, line_count=0) + _make_history(4, line_count=100)
    assert "unexpected_empty" not in evaluate_run(run, history, _DEFAULT_CFG)


def test_unexpected_empty_does_not_fire_when_line_count_is_nonzero() -> None:
    run = _make_run(line_count=1)
    history = _make_history(10, line_count=100)
    assert "unexpected_empty" not in evaluate_run(run, history, _DEFAULT_CFG)


def test_unexpected_empty_skipped_when_history_line_counts_all_none() -> None:
    run = _make_run(line_count=0)
    history = [replace(r, line_count=None) for r in _make_history(10, line_count=100)]
    assert "unexpected_empty" not in evaluate_run(run, history, _DEFAULT_CFG)


# ---------------------------------------------------------------------------
# Rule: new_failure — fires
# ---------------------------------------------------------------------------


def test_new_failure_fires_when_state_fail_and_all_history_ok() -> None:
    # exit_code=None so exit_code_changed doesn't also fire — isolates new_failure
    run = _make_run(state="fail", exit_code=None)
    history = _make_history(10, state="ok", exit_code=None)
    flags = evaluate_run(run, history, _DEFAULT_CFG)
    assert "new_failure" in flags


# ---------------------------------------------------------------------------
# Rule: new_failure — no-fire
# ---------------------------------------------------------------------------


def test_new_failure_does_not_fire_when_history_has_prior_failure() -> None:
    run = _make_run(state="fail", exit_code=1)
    history = _make_history(9, state="ok") + _make_history(1, state="fail")
    assert "new_failure" not in evaluate_run(run, history, _DEFAULT_CFG)


def test_new_failure_does_not_fire_when_state_is_ok() -> None:
    run = _make_run(state="ok")
    history = _make_history(10, state="ok")
    assert "new_failure" not in evaluate_run(run, history, _DEFAULT_CFG)


def test_new_failure_does_not_fire_with_empty_history_after_gate() -> None:
    """new_failure rule itself requires non-empty history."""
    cfg = CronAnomalyConfig(min_history=0, rolling_window=20, duration_k=4.0, output_band=0.5)
    run = _make_run(state="fail")
    # min_history=0 so gate passes, but history is empty → rule returns None
    assert "new_failure" not in evaluate_run(run, [], cfg)


# ---------------------------------------------------------------------------
# Multiple rules fire simultaneously
# ---------------------------------------------------------------------------


def test_multiple_rules_fire_returns_comma_joined_in_stable_order() -> None:
    """When several rules fire, output is comma-joined in declaration order."""
    # Make run that trips duration_outlier AND new_failure (and exit_code_changed too)
    run = _make_run(state="fail", exit_code=1, duration_seconds=9999.0)
    history = _make_history(10, state="ok", exit_code=0, duration_seconds=10.0)
    flags = evaluate_run(run, history, _DEFAULT_CFG)
    parts = flags.split(",")
    # Check expected flags present
    assert "duration_outlier" in parts
    assert "exit_code_changed" in parts
    assert "new_failure" in parts
    # Check stable declaration order
    dur_idx = parts.index("duration_outlier")
    ec_idx = parts.index("exit_code_changed")
    nf_idx = parts.index("new_failure")
    assert dur_idx < ec_idx < nf_idx


# ---------------------------------------------------------------------------
# Edge: line_count NULL in history (skip from output-size statistics)
# ---------------------------------------------------------------------------


def test_line_count_none_in_history_skipped_for_output_size_rules() -> None:
    """History entries with line_count=None are excluded from size stats."""
    run = _make_run(line_count=300)
    # Mix: 5 with line_count=100, 5 with line_count=None
    history = _make_history(5, line_count=100) + [
        replace(r, line_count=None) for r in _make_history(5, line_count=100)
    ]
    # With median of non-None values = 100, 300 > 100*1.5 → spike fires
    flags = evaluate_run(run, history, _DEFAULT_CFG)
    assert "output_size_spike" in flags


# ---------------------------------------------------------------------------
# Edge: evaluate_run with exit_code=None in run
# ---------------------------------------------------------------------------


def test_evaluate_run_exit_code_none_in_run_no_crash() -> None:
    run = _make_run(exit_code=None)
    history = _make_history(10, exit_code=0)
    # Should complete without error; exit_code_changed cannot fire
    result = evaluate_run(run, history, _DEFAULT_CFG)
    assert "exit_code_changed" not in result


# ---------------------------------------------------------------------------
# Rule: duration_outlier — p95 <= 0 branch (run_anomaly.py _rule_duration_outlier)
# ---------------------------------------------------------------------------


def test_duration_outlier_skipped_when_p95_is_zero() -> None:
    """p95 <= 0 (all history durations = 0.0) → rule returns None, no flag."""
    run = _make_run(duration_seconds=999.0)
    history = _make_history(10, duration_seconds=0.0)
    result = _rule_duration_outlier(run, history, k=4.0)  # pyright: ignore[reportPrivateUsage]
    assert result is None


# ---------------------------------------------------------------------------
# Rule: output_size_spike — defensive _median=None branch (run_anomaly.py:125)
# ---------------------------------------------------------------------------


def test_rule_output_size_spike_returns_none_when_median_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_rule_output_size_spike returns None when _median returns None (defensive guard)."""
    import homelab_monitor.kernel.cron.run_anomaly as _mod  # noqa: PLC0415

    def _none_median(_values: object) -> None:
        return None

    monkeypatch.setattr(_mod, "_median", _none_median)
    run = _make_run(line_count=5)
    history = _make_history(3, line_count=10)
    result = _rule_output_size_spike(  # pyright: ignore[reportPrivateUsage]
        run, history, 0.5
    )
    assert result is None
