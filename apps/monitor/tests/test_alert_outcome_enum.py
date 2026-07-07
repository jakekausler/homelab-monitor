"""Tests for the EPIC-010 additions to ``AlertOutcome``."""

from __future__ import annotations

from homelab_monitor.kernel.alerts.types import AlertOutcome

# STAGE-010-002 added AUTO_RESOLVED + MAINTENANCE_SUPPRESSED on top of the
# pre-existing 4 members; assert the pre-existing 4 remain intact (regression
# guard) rather than a total-count that churns whenever a new member ships.
PRE_EPIC_010_ALERT_OUTCOME_MEMBERS = {"acked", "dismissed", "auto_fixed", "escalated"}


def test_alert_outcome_has_new_members() -> None:
    """AlertOutcome gains AUTO_RESOLVED and MAINTENANCE_SUPPRESSED (STAGE-010-002)."""
    assert AlertOutcome.AUTO_RESOLVED.value == "auto_resolved"
    assert AlertOutcome.MAINTENANCE_SUPPRESSED.value == "maintenance_suppressed"
    assert PRE_EPIC_010_ALERT_OUTCOME_MEMBERS.issubset({m.value for m in AlertOutcome})
