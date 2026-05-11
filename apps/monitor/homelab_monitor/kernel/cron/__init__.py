"""STAGE-002-002: cron registry CRUD subsystem.

This package owns:
- ``CronRecord`` (the canonical hydrated row dataclass; HeartbeatRepo
  imports from here as of STAGE-002-002)
- ``CronRepo`` (write-side CRUD: list, get, create, update, soft-delete,
  restore — all dual-writing to ``audit_log`` in the same transaction)
- Pydantic request/response schemas for the ``/api/crons`` router
- Pure schedule helpers (``canonicalize_schedule``, ``compute_next_runs``)

The HeartbeatRepo (STAGE-002-001) keeps its read paths but delegates the
``CronRecord`` definition to this package to avoid a domain split.
"""
