"""STAGE-009-006: runbook_run_approvals table for the dry-run approval flow.

A risky runbook (dry_run_required=1) runs claude PLAN-ONLY, stores the plan as a
runbook_runs row (mode='dry_run'), and creates a PENDING approval pinned to the
runbook content_hash. An explicit, audited operator approval is required before
the real run. This table records those approvals.

status is a plain TEXT column ('pending'|'approved'|'rejected') with NO CHECK
constraint (application-enforced). FKs point at runbook_runs.id (dry + real).

Fresh table → plain op.create_table (no batch_alter_table needed; the FKs are
declared inline). Downgrade drops the indexes then the table.

Revision ID: 0047
Revises: 0046
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "runbook_run_approvals",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("dry_run_id", sa.Text(), nullable=False),
        sa.Column("runbook_id", sa.Text(), nullable=False),
        sa.Column("alert_id", sa.Text(), nullable=True),
        sa.Column("pinned_runbook_hash", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.Text(), nullable=True),
        sa.Column("real_run_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["dry_run_id"],
            ["runbook_runs.id"],
            name="fk_runbook_run_approvals_dry_run_id",
        ),
        sa.ForeignKeyConstraint(
            ["real_run_id"],
            ["runbook_runs.id"],
            name="fk_runbook_run_approvals_real_run_id",
        ),
    )
    op.create_index(
        "ix_runbook_run_approvals_status",
        "runbook_run_approvals",
        ["status"],
    )
    op.create_index(
        "ix_runbook_run_approvals_dry_run_id",
        "runbook_run_approvals",
        ["dry_run_id"],
    )
    op.create_index(
        "ix_runbook_run_approvals_runbook_id",
        "runbook_run_approvals",
        ["runbook_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_runbook_run_approvals_runbook_id",
        table_name="runbook_run_approvals",
    )
    op.drop_index(
        "ix_runbook_run_approvals_dry_run_id",
        table_name="runbook_run_approvals",
    )
    op.drop_index(
        "ix_runbook_run_approvals_status",
        table_name="runbook_run_approvals",
    )
    op.drop_table("runbook_run_approvals")
