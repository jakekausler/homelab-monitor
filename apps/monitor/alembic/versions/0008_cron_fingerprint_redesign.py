"""STAGE-002-003: cron registry — fingerprint identity, drop integration_mode.

This is a DESTRUCTIVE migration in BOTH directions: ``upgrade()`` drops the
existing ``crons`` and ``heartbeats_state`` tables and recreates them with the
fingerprint-keyed schema; ``downgrade()`` drops the new tables and recreates
the legacy UUID-keyed shape. **All cron rows and heartbeat state are lost**
on either direction. The dev rig has only seed data; there is no production
DB to preserve (per session note). Production rollback uses backup
restoration, not ``alembic downgrade``.

Schema redesign (per the 2026-05-11 cron-derived-state-redesign spec):
- ``crons.fingerprint`` (TEXT PK) replaces ``crons.id`` (UUIDv7 PK). The
  fingerprint is SHA256 hex of ``json.dumps({host, source_path, schedule,
  command}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)``,
  matching the ``alerts.fingerprinting.quarantine_fingerprint`` precedent.
- ``integration_mode`` is removed entirely (the derived-state model runs both
  log-scrape and heartbeat collectors when present; there is no longer a
  "mode" axis).
- ``archived_at`` is renamed ``hidden_at`` (semantic: "hide from default views
  and suppress alerts"; not the same as "delete from disk").
- ``source_path TEXT NULL`` is added (e.g., ``/etc/crontab``,
  ``/etc/cron.d/foo``, ``crontab:<user>``; NULL for remote-only crons
  registered by a wrapper from an un-scannable host).
- ``wrapper_installed_at TEXT NULL`` is added (set by ``/register`` when
  the wrapper is installed; gates wrapper-health alerts in STAGE-002-010).
- ``heartbeats_state.cron_id`` becomes ``heartbeats_state.cron_fingerprint``
  (TEXT PK FK to ``crons.fingerprint`` ON DELETE CASCADE).

Seed rows: four demo crons inserted via ``op.bulk_insert`` to populate the
fresh dev DB. Fingerprints are PRECOMPUTED below and were validated by
``test_fingerprint_computation.py`` against the same algorithm. NO audit_log
rows are emitted for the seeds — D5 chose "infra event, not user action."

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import hashlib
import json
import os

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


# ---------------------------------------------------------------------------
# Migration-local fingerprint helper (DO NOT import from kernel).
# ---------------------------------------------------------------------------


def _compute_fingerprint(host: str, source_path: str | None, schedule: str, command: str) -> str:
    """SHA256 over a JSON-canonical dict.

    Migration-local copy of ``kernel.cron.fingerprint.compute_fingerprint``.
    Re-implemented here because migrations must be runnable even if the
    kernel module evolves or is renamed. Mirrors the F19 pattern used by
    ``alerts.fingerprinting.quarantine_fingerprint``: serializing via JSON
    rather than a delimiter-joined string prevents collision in the
    presence of payload values containing the delimiter.

    NULL ``source_path`` serializes as JSON ``null``, which is intentionally
    distinct from the empty string ``""`` (per D2+D4 interaction).
    """
    payload = json.dumps(
        {
            "host": host,
            "source_path": source_path,
            "schedule": schedule,
            "command": command,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Demo seed rows (per D5: NO audit rows emitted).
# Fingerprints precomputed offline and verified by
# tests/test_fingerprint_computation.py against the kernel helper.
# Four rows mirror the legacy STAGE-002-002 demo (observe / heartbeat / both /
# stale) but the mode column is gone — the rows differ by command identity
# and last_seen_state alone, which is exactly the derived-state model.
# ---------------------------------------------------------------------------

# Static demo timestamp keeps the migration deterministic across hosts.
_DEMO_NOW = "2026-05-12T00:00:00+00:00"

_SEED_ROWS = [
    {
        "fingerprint": "9455960fd5210182e96ff98baf929d9de0be4ba52766e6f5b02ea5e612cd7d86",
        "name": "observe-job.sh",
        "host": "homelab-host",
        "command": "/opt/scripts/observe-job.sh",
        "schedule": "*/5 * * * *",
        "schedule_canonical": "*/5 * * * *",
        "cadence_seconds": 300,
        "expected_grace_seconds": 300,
        "enabled": 1,
        "last_seen_state": "unknown",
        "created_at": _DEMO_NOW,
        "updated_at": _DEMO_NOW,
        "hidden_at": None,
        "source_path": "/etc/crontab",
        "wrapper_installed_at": None,
    },
    {
        "fingerprint": "532041fb1598f9cfb40e08dc8aec07ddce99e6fb3001d81124a3b71e148e64d9",
        "name": "heartbeat-job.sh",
        "host": "homelab-host",
        "command": "/opt/scripts/heartbeat-job.sh",
        "schedule": "0 * * * *",
        "schedule_canonical": "0 * * * *",
        "cadence_seconds": 3600,
        "expected_grace_seconds": 300,
        "enabled": 1,
        "last_seen_state": "ok",
        "created_at": _DEMO_NOW,
        "updated_at": _DEMO_NOW,
        "hidden_at": None,
        "source_path": "/etc/cron.d/heartbeat-demo",
        "wrapper_installed_at": _DEMO_NOW,
    },
    {
        "fingerprint": "23d48f4bb8f816d34bc523805ab7161d368f2a108314c7d079fa610aa93359d2",
        "name": "both-job.sh",
        "host": "homelab-host",
        "command": "/opt/scripts/both-job.sh",
        "schedule": "@daily",
        "schedule_canonical": "0 0 * * *",
        "cadence_seconds": 86400,
        "expected_grace_seconds": 300,
        "enabled": 1,
        "last_seen_state": "ok",
        "created_at": _DEMO_NOW,
        "updated_at": _DEMO_NOW,
        "hidden_at": None,
        "source_path": "/etc/cron.d/both-demo",
        "wrapper_installed_at": _DEMO_NOW,
    },
    {
        "fingerprint": "c54a8658dc597650761c7efebddde49794dc19275c8b125750606f3a6a11bc30",
        "name": "stale-job.sh",
        "host": "remote-host",
        "command": "/opt/remote/stale-job.sh",
        "schedule": "*/15 * * * *",
        "schedule_canonical": "*/15 * * * *",
        "cadence_seconds": 900,
        "expected_grace_seconds": 300,
        "enabled": 1,
        "last_seen_state": "failed",
        "created_at": _DEMO_NOW,
        "updated_at": _DEMO_NOW,
        "hidden_at": None,
        "source_path": None,
        "wrapper_installed_at": _DEMO_NOW,
    },
]


def _assert_seed_fingerprints_match() -> None:
    """Defensive: recompute fingerprints at migration time and assert match.

    Catches accidental edits to host/command/schedule/source_path that would
    leave the seeded fingerprint stale. Migration aborts before any DDL
    runs if the constants drift.
    """
    for row in _SEED_ROWS:
        expected = _compute_fingerprint(
            host=row["host"],  # type: ignore[arg-type]
            source_path=row["source_path"],  # type: ignore[arg-type]
            schedule=row["schedule"],  # type: ignore[arg-type]
            command=row["command"],  # type: ignore[arg-type]
        )
        if expected != row["fingerprint"]:
            msg = (
                f"seed row fingerprint drift: expected {expected!r}, "
                f"have {row['fingerprint']!r}; row={row}"
            )
            raise RuntimeError(msg)


def upgrade() -> None:
    _assert_seed_fingerprints_match()

    # --- 1. Drop the FK-dependent table first ---
    op.drop_table("heartbeats_state")

    # --- 2. Drop legacy crons + its index (idx_crons_active from 0007 lives on the
    # old shape; the recreated crons table will get its own idx_crons_active below). ---
    op.drop_index("idx_crons_active", table_name="crons")
    op.drop_index("ix_crons_integration_mode_enabled", table_name="crons")
    op.drop_table("crons")

    # --- 3. Create crons with the new schema. ---
    op.create_table(
        "crons",
        sa.Column("fingerprint", sa.Text(), primary_key=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("schedule", sa.Text(), nullable=True),
        sa.Column("schedule_canonical", sa.Text(), nullable=True),
        sa.Column("cadence_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "expected_grace_seconds",
            sa.Integer(),
            nullable=False,
            server_default="300",
        ),
        sa.Column("enabled", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "last_seen_state",
            sa.Text(),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("hidden_at", sa.Text(), nullable=True),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("wrapper_installed_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "last_seen_state IN ('unknown', 'running', 'ok', 'failed', 'late')",
            name="ck_crons_last_seen_state",
        ),
        sa.CheckConstraint(
            "(schedule IS NOT NULL AND schedule != '') OR cadence_seconds > 0",
            name="ck_crons_schedule_xor_cadence",
        ),
    )

    # Speedy lookup for log-scrape matching (STAGE-002-008).
    op.create_index("ix_crons_host_command", "crons", ["host", "command"])
    # Partial index for fast active-only list queries (mirror of 0007's idea).
    op.create_index(
        "idx_crons_active",
        "crons",
        ["name"],
        sqlite_where=text("hidden_at IS NULL"),
    )

    # --- 4. Recreate heartbeats_state keyed by cron_fingerprint. ---
    op.create_table(
        "heartbeats_state",
        sa.Column(
            "cron_fingerprint",
            sa.Text(),
            sa.ForeignKey("crons.fingerprint", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "current_state",
            sa.Text(),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("last_start_at", sa.Text(), nullable=True),
        sa.Column("last_ok_at", sa.Text(), nullable=True),
        sa.Column("last_fail_at", sa.Text(), nullable=True),
        sa.Column(
            "current_streak",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("expected_next_at", sa.Text(), nullable=True),
        sa.Column("last_duration_seconds", sa.Float(), nullable=True),
        sa.Column("last_exit_code", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "current_state IN ('unknown', 'running', 'ok', 'failed', 'late')",
            name="ck_heartbeats_state_current_state",
        ),
    )

    # --- 5. Bulk-insert the 4 demo rows (no audit_log per D5). ---
    # Only seeded when HOMELAB_MONITOR_INCLUDE_DEMO_SEEDS=1 (dev rig only).
    # Tests do not set this var; production never sets it.
    if os.getenv("HOMELAB_MONITOR_INCLUDE_DEMO_SEEDS") == "1":
        crons_table = sa.table(
            "crons",
            sa.column("fingerprint", sa.Text),
            sa.column("name", sa.Text),
            sa.column("host", sa.Text),
            sa.column("command", sa.Text),
            sa.column("schedule", sa.Text),
            sa.column("schedule_canonical", sa.Text),
            sa.column("cadence_seconds", sa.Integer),
            sa.column("expected_grace_seconds", sa.Integer),
            sa.column("enabled", sa.Integer),
            sa.column("last_seen_state", sa.Text),
            sa.column("created_at", sa.Text),
            sa.column("updated_at", sa.Text),
            sa.column("hidden_at", sa.Text),
            sa.column("source_path", sa.Text),
            sa.column("wrapper_installed_at", sa.Text),
        )
        op.bulk_insert(crons_table, _SEED_ROWS)


def downgrade() -> None:
    """Restore the legacy shape. DROPS ALL DATA (symmetric with upgrade).

    The legacy seed pattern (STAGE-002-002's dev seed) is NOT re-inserted on
    downgrade — production rollback uses backup restoration, and the dev
    rig can be wiped with ``make dev-clean``.
    """
    op.drop_table("heartbeats_state")
    op.drop_index("idx_crons_active", table_name="crons")
    op.drop_index("ix_crons_host_command", table_name="crons")
    op.drop_table("crons")

    # Recreate legacy crons shape (UUID PK + integration_mode + archived_at).
    op.create_table(
        "crons",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, server_default=""),
        sa.Column("host", sa.Text(), nullable=False, server_default=""),
        sa.Column("command", sa.Text(), nullable=False, server_default=""),
        sa.Column("schedule", sa.Text(), nullable=False, server_default=""),
        sa.Column("schedule_canonical", sa.Text(), nullable=True),
        sa.Column("cadence_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "expected_grace_seconds",
            sa.Integer(),
            nullable=False,
            server_default="300",
        ),
        sa.Column(
            "integration_mode",
            sa.Text(),
            nullable=False,
            server_default="observe",
        ),
        sa.Column("enabled", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "last_seen_state",
            sa.Text(),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("created_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("archived_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "integration_mode IN ('observe', 'heartbeat', 'both')",
            name="ck_crons_integration_mode",
        ),
        sa.CheckConstraint(
            "last_seen_state IN ('unknown', 'running', 'ok', 'failed', 'late')",
            name="ck_crons_last_seen_state",
        ),
        sa.CheckConstraint(
            "schedule != '' OR cadence_seconds > 0",
            name="ck_crons_schedule_xor_cadence",
        ),
    )
    op.create_index(
        "ix_crons_integration_mode_enabled",
        "crons",
        ["integration_mode", "enabled"],
    )
    op.create_index(
        "idx_crons_active",
        "crons",
        ["name"],
        sqlite_where=text("archived_at IS NULL"),
    )

    # Recreate legacy heartbeats_state keyed by cron_id.
    op.create_table(
        "heartbeats_state",
        sa.Column(
            "cron_id",
            sa.Text(),
            sa.ForeignKey("crons.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "current_state",
            sa.Text(),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("last_start_at", sa.Text(), nullable=True),
        sa.Column("last_ok_at", sa.Text(), nullable=True),
        sa.Column("last_fail_at", sa.Text(), nullable=True),
        sa.Column(
            "current_streak",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("expected_next_at", sa.Text(), nullable=True),
        sa.Column("last_duration_seconds", sa.Float(), nullable=True),
        sa.Column("last_exit_code", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "current_state IN ('unknown', 'running', 'ok', 'failed', 'late')",
            name="ck_heartbeats_state_current_state",
        ),
    )
