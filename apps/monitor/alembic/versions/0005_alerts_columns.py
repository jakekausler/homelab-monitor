"""STAGE-001-013: Add alerts + alert_outcomes behavioural columns and indexes.

Expands the SCAFFOLDING-stage tables created in 0001_initial_schema.py to their
spec §6.1 shape:

- alerts: source_tool, severity, status, opened_at, last_seen_at, resolved_at,
  ack_at, ack_by (FK->users.id), runbook_id, payload_json
- alert_outcomes: outcome, decided_at, decided_by (FK->users.id)

Indexes added (spec §6.1 + listing performance):
- ix_alerts_source_tool_opened_at  on alerts(source_tool, opened_at)
- ix_alerts_status_opened_at       on alerts(status, opened_at)
- ux_alerts_fingerprint_firing     UNIQUE partial index on alerts(fingerprint)
                                   WHERE status = 'firing'
                                   (race-safe dedup of concurrent firing
                                   inserts; resolved rows are excluded so a
                                   re-fire after resolve creates a new row)
- ix_alert_outcomes_alert_id       on alert_outcomes(alert_id)

The pre-existing idx_alerts_fingerprint (created in 0001) remains.

Tables are empty stubs; no backfill is needed. New columns are nullable except
where defaulted ("status"/"severity"/"source_tool"/"opened_at"/"last_seen_at"/
"payload_json" are nullable in the schema so existing stub rows — if any — do
not violate NOT NULL; the AlertRepository ALWAYS writes them on insert).

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ----- alerts -----
    # Use batch_alter_table for SQLite compatibility with foreign keys
    with op.batch_alter_table("alerts") as batch_op:
        batch_op.add_column(sa.Column("source_tool", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("severity", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("status", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("opened_at", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("last_seen_at", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("resolved_at", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("ack_at", sa.Text(), nullable=True))
        # Add column without FK constraint in batch; add FK after
        batch_op.add_column(sa.Column("ack_by", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("runbook_id", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("payload_json", sa.Text(), nullable=True))
        # Add FK constraint with explicit name
        batch_op.create_foreign_key(
            "fk_alerts_ack_by_users",
            "users",
            ["ack_by"],
            ["id"],
        )
        batch_op.create_index(
            "ix_alerts_source_tool_opened_at",
            ["source_tool", "opened_at"],
        )
        batch_op.create_index(
            "ix_alerts_status_opened_at",
            ["status", "opened_at"],
        )
        # Race-safe dedup: concurrent inserts of the same fingerprint while a
        # row is already firing collide on this unique partial index. The
        # ingest path catches IntegrityError and re-reads the existing row.
        # Resolved rows are excluded so a re-fire after resolve still creates
        # a new row (intentional: distinct lifecycle events).
        batch_op.create_index(
            "ux_alerts_fingerprint_firing",
            ["fingerprint"],
            unique=True,
            sqlite_where=sa.text("status = 'firing'"),
        )

    # ----- alert_outcomes -----
    with op.batch_alter_table("alert_outcomes") as batch_op:
        batch_op.add_column(sa.Column("outcome", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("decided_at", sa.Text(), nullable=True))
        # Add column without FK constraint in batch; add FK after
        batch_op.add_column(sa.Column("decided_by", sa.Integer(), nullable=True))
        # Add FK constraint with explicit name
        batch_op.create_foreign_key(
            "fk_alert_outcomes_decided_by_users",
            "users",
            ["decided_by"],
            ["id"],
        )
        batch_op.create_index(
            "ix_alert_outcomes_alert_id",
            ["alert_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("alert_outcomes") as batch_op:
        batch_op.drop_index("ix_alert_outcomes_alert_id")
        batch_op.drop_column("decided_by")
        batch_op.drop_column("decided_at")
        batch_op.drop_column("outcome")

    with op.batch_alter_table("alerts") as batch_op:
        batch_op.drop_index("ux_alerts_fingerprint_firing")
        batch_op.drop_index("ix_alerts_status_opened_at")
        batch_op.drop_index("ix_alerts_source_tool_opened_at")
        batch_op.drop_column("payload_json")
        batch_op.drop_column("runbook_id")
        batch_op.drop_column("ack_by")
        batch_op.drop_column("ack_at")
        batch_op.drop_column("resolved_at")
        batch_op.drop_column("last_seen_at")
        batch_op.drop_column("opened_at")
        batch_op.drop_column("status")
        batch_op.drop_column("severity")
        batch_op.drop_column("source_tool")
