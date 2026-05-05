"""Async scheduler for in-process Python collectors.

Public surface re-exported here:

- :class:`Scheduler` — the per-collector tick driver.
- :class:`SchedulerConfig` — pool sizes + shutdown grace.
- :class:`FailureBudget` — per-collector failure tracking + quarantine.
"""

from __future__ import annotations

from homelab_monitor.kernel.scheduler.failure_budget import FailureBudget
from homelab_monitor.kernel.scheduler.scheduler import Scheduler, SchedulerConfig

__all__ = ["FailureBudget", "Scheduler", "SchedulerConfig"]
