"""STAGE-003-007 C1: add exec_authorized column to probe_targets.

Persists the per-row exec authorization so ProbeSupervisor can read it
source-agnostically (label path and file_override path both write the bit).

Revision ID: 0025_probe_targets_exec_authorized
Revises: 0024
"""

from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision: str = "0025_probe_targets_exec_authorized"
down_revision: str | None = "0024"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        text("ALTER TABLE probe_targets ADD COLUMN exec_authorized INTEGER NOT NULL DEFAULT 0")
    )


def downgrade() -> None:
    # SQLite ≥ 3.35 supports DROP COLUMN.
    op.execute(text("ALTER TABLE probe_targets DROP COLUMN exec_authorized"))
