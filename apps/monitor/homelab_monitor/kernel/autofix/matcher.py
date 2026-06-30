"""Alert -> runbook match logic (STAGE-009-005, choice B)."""

from __future__ import annotations

from homelab_monitor.kernel.alerts.types import Alert
from homelab_monitor.kernel.runbooks.config import AlertMatcher
from homelab_monitor.kernel.runbooks.repository import RunbookRecord


def _matcher_matches(matcher: AlertMatcher, alert: Alert) -> bool:
    """True iff this single matcher matches the alert.

    alertname predicate: matcher.alertname is None OR equals
    alert.labels.get("alertname"). labels predicate: every (k, v) in
    matcher.labels is present in alert.labels with equal value.
    """
    if matcher.alertname is not None and matcher.alertname != alert.labels.get("alertname"):
        return False
    return all(alert.labels.get(key) == value for key, value in matcher.labels.items())


def _runbook_matches(record: RunbookRecord, alert: Alert) -> bool:
    """True iff ANY of the runbook's matchers match the alert."""
    for pattern in record.alert_match_patterns:
        matcher = AlertMatcher.model_validate(pattern)
        if _matcher_matches(matcher, alert):
            return True
    return False


def matching_runbooks(records: list[RunbookRecord], alert: Alert) -> list[RunbookRecord]:
    """Return all runbooks whose matchers match the alert (order preserved)."""
    return [r for r in records if _runbook_matches(r, alert)]
