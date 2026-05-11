"""Unit tests for Cron schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from homelab_monitor.kernel.cron.schemas import CronUpdate


def test_cron_update_schema_both_set_raises() -> None:
    """CronUpdate(schedule=..., cadence_seconds=...) raises (covers schemas.py:190-191)."""
    with pytest.raises(ValidationError, match="at most one"):
        CronUpdate(schedule="* * * * *", cadence_seconds=60)


def test_cron_update_schema_neither_set_raises() -> None:
    """CronUpdate with both schedule='' and cadence_seconds=0 raises (covers schemas.py:193-194)."""
    with pytest.raises(ValidationError, match="at least one"):
        CronUpdate(schedule="", cadence_seconds=0)
