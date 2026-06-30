"""STAGE-009-005: indexes for runbook_runs rate-limit/cooldown/inflight queries.

Adds composite indexes supporting the auto-fix orchestrator's gate queries:
- (runbook_id, started_at): count_inflight + count_started_since (rate limit).
- (runbook_id, ended_at): latest_ended_at (cooldown).

Revision ID: 0046
Revises: 0045
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_indexes = {ix["name"] for ix in inspector.get_indexes("runbook_runs")}

    if "ix_runbook_runs_runbook_id_started_at" not in existing_indexes:
        op.execute(
            text(
                "CREATE INDEX ix_runbook_runs_runbook_id_started_at "
                "ON runbook_runs(runbook_id, started_at)"
            )
        )
    if "ix_runbook_runs_runbook_id_ended_at" not in existing_indexes:
        op.execute(
            text(
                "CREATE INDEX ix_runbook_runs_runbook_id_ended_at "
                "ON runbook_runs(runbook_id, ended_at)"
            )
        )


def downgrade() -> None:
    op.execute(text("DROP INDEX IF EXISTS ix_runbook_runs_runbook_id_ended_at"))
    op.execute(text("DROP INDEX IF EXISTS ix_runbook_runs_runbook_id_started_at"))
