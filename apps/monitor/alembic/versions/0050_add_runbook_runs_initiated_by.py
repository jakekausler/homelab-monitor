"""add initiated_by column to runbook_runs

Revision ID: 0050
Revises: 0049
Create Date: 2026-07-02 19:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Two separate batches: SQLite batch mode recreates the whole table per
    # batch using the FINAL column schema for that batch, so add_column and
    # alter_column(drop default) must NOT be combined into one batch — doing
    # so drops the default before the row-copy step, breaking the backfill.
    with op.batch_alter_table("runbook_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "initiated_by",
                sa.Text(),
                nullable=False,
                server_default="alert",
            )
        )
    with op.batch_alter_table("runbook_runs", schema=None) as batch_op:
        batch_op.alter_column("initiated_by", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("runbook_runs", schema=None) as batch_op:
        batch_op.drop_column("initiated_by")
