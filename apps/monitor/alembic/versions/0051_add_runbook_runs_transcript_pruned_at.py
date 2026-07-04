"""add transcript_pruned_at column to runbook_runs

Revision ID: 0051
Revises: 0050
Create Date: 2026-07-03 22:00:00.000000

Additive nullable column. Set by the transcript rotator when a transcript
file is deleted; the runbook_runs audit row is NEVER deleted (STAGE-009-012
non-negotiable #4).

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("runbook_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "transcript_pruned_at",
                sa.Text(),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("runbook_runs", schema=None) as batch_op:
        batch_op.drop_column("transcript_pruned_at")
