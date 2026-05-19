"""Rule-based anomaly evaluator for cron runs (STAGE-002-014).

Pure-Python heuristics over ``cron_runs`` metadata. NO log-content analysis,
NO ML. Every rule is gated by ``CronAnomalyConfig.min_history`` — a flag does
not fire until enough baseline history has accumulated.

v1 RULES (all six):
- duration_outlier  — duration_seconds > k * rolling_p95(history.duration_seconds)
- exit_code_changed — exit_code != dominant_exit_code(history)
- output_size_spike — line_count > median(history.line_count) * (1 + band)
- output_size_drop  — line_count < median(history.line_count) * (1 - band)
- unexpected_empty  — line_count == 0 AND MOST history runs had line_count > 0
- new_failure       — state == 'fail' AND ALL history runs were state == 'ok'

CONTENT_DIGEST IS NOT CONSUMED HERE in v1. The reconciler still computes the
digest (a forward investment) but no v1 rule uses it. EPIC-004's content
clustering will consume it later.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence

from homelab_monitor.kernel.config import CronAnomalyConfig
from homelab_monitor.kernel.cron.run_repository import CronRunRecord

_MIN_QUANTILE_VALUES = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _p95(values: Sequence[float]) -> float | None:
    """Return the 95th-percentile value of ``values``, or None if too few.

    ``statistics.quantiles(n=20)`` returns 19 cut-points; index 18 is the
    p95. The function requires at least 2 data points; with only 1 we fall
    back to that single value (one history run is degenerate but should
    not crash).
    """
    if not values:
        return None
    if len(values) < _MIN_QUANTILE_VALUES:
        return values[0]
    cuts = statistics.quantiles(values, n=20, method="inclusive")
    return cuts[18]


def _median(values: Sequence[float]) -> float | None:
    """Return median, or None for an empty sequence."""
    if not values:
        return None
    return statistics.median(values)


def _dominant_exit_code(history: Sequence[CronRunRecord]) -> int | None:
    """Return the most-common non-None exit_code in history, or None.

    Ties broken by the lower exit_code (deterministic). An all-None history
    returns None (no signal — exit_code_changed cannot fire).
    """
    codes = [r.exit_code for r in history if r.exit_code is not None]
    if not codes:
        return None
    counter = Counter(codes)
    # Counter.most_common is order-stable for ties; we want determinism
    # across Python versions. Sort by (-count, code) so the LOWER code wins
    # on a tie.
    return min(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0]


# ---------------------------------------------------------------------------
# Per-rule evaluators (each returns the flag string or None)
# ---------------------------------------------------------------------------


def _rule_duration_outlier(
    run: CronRunRecord,
    history: Sequence[CronRunRecord],
    k: float,
) -> str | None:
    if run.duration_seconds is None:
        return None
    durations = [r.duration_seconds for r in history if r.duration_seconds is not None]
    if not durations:
        return None
    p95 = _p95(durations)
    if p95 is None or p95 <= 0:
        return None
    if run.duration_seconds > k * p95:
        return "duration_outlier"
    return None


def _rule_exit_code_changed(
    run: CronRunRecord,
    history: Sequence[CronRunRecord],
) -> str | None:
    if run.exit_code is None:
        return None
    dominant = _dominant_exit_code(history)
    if dominant is None:
        return None
    if run.exit_code != dominant:
        return "exit_code_changed"
    return None


def _rule_output_size_spike(
    run: CronRunRecord,
    history: Sequence[CronRunRecord],
    band: float,
) -> str | None:
    if run.line_count is None:
        return None
    lcs = [float(r.line_count) for r in history if r.line_count is not None]
    if not lcs:
        return None
    med = _median(lcs)
    if med is None:
        return None
    if med == 0:
        return "output_size_spike" if run.line_count > 0 else None
    if run.line_count > med * (1 + band):
        return "output_size_spike"
    return None


def _rule_output_size_drop(
    run: CronRunRecord,
    history: Sequence[CronRunRecord],
    band: float,
) -> str | None:
    if run.line_count is None:
        return None
    lcs = [float(r.line_count) for r in history if r.line_count is not None]
    if not lcs:
        return None
    med = _median(lcs)
    if med is None or med == 0:
        # median of 0 means history is mostly empty; drop is meaningless.
        return None
    if run.line_count < med * (1 - band):
        return "output_size_drop"
    return None


def _rule_unexpected_empty(
    run: CronRunRecord,
    history: Sequence[CronRunRecord],
) -> str | None:
    """``line_count == 0`` AND MOST history runs had line_count > 0.

    "Most" = strictly more than half of history runs with a non-None
    line_count had a positive count. A cron that legitimately runs silent
    will never trip this rule because its history will be majority zeros.
    """
    if run.line_count != 0:
        return None
    lcs = [r.line_count for r in history if r.line_count is not None]
    if not lcs:
        return None
    non_empty = sum(1 for c in lcs if c > 0)
    if non_empty * 2 > len(lcs):  # strictly more than half
        return "unexpected_empty"
    return None


def _rule_new_failure(
    run: CronRunRecord,
    history: Sequence[CronRunRecord],
) -> str | None:
    """``state == 'fail'`` AND ALL history runs were ``state == 'ok'``.

    A single non-ok run in history defuses the rule (the failure is not new).
    """
    if run.state != "fail":
        return None
    if not history:
        return None
    if all(r.state == "ok" for r in history):
        return "new_failure"
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate_run(
    run: CronRunRecord,
    history: Sequence[CronRunRecord],
    config: CronAnomalyConfig,
) -> str:
    """Return the comma-separated ``anomaly_flags`` string for ``run``.

    ``history`` is the LAST N COMPLETED (``state != 'running'``) runs of the
    SAME cron, newest-first, EXCLUDING ``run`` itself. The caller is
    responsible for that filtering (the repository's
    ``list_recent_completed`` does it).

    Returns the empty string when no rules fire OR when
    ``len(history) < config.min_history`` (the min-history gate).

    Rules are checked in a stable order so the joined flag string is
    deterministic across runs.

    NOTE: ``content_digest`` is NOT consumed in v1. EPIC-004 will.
    """
    if len(history) < config.min_history:
        return ""
    flags: list[str] = []
    checks: list[str | None] = [
        _rule_duration_outlier(run, history, config.duration_k),
        _rule_exit_code_changed(run, history),
        _rule_output_size_spike(run, history, config.output_band),
        _rule_output_size_drop(run, history, config.output_band),
        _rule_unexpected_empty(run, history),
        _rule_new_failure(run, history),
    ]
    for flag in checks:
        if flag is not None:
            flags.append(flag)
    return ",".join(flags)


__all__ = [
    "evaluate_run",
]
