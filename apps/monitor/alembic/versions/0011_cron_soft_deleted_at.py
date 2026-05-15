"""STAGE-002-007A: add crons.soft_deleted_at column.

UTC ISO-8601 timestamp; NULL means the cron is active. A non-NULL value means
a discovery scan cleanly inspected the cron's source file and the cron's
fingerprint was absent — the system's signal that the cron was deleted on
disk. Cleared (set back to NULL) when a later clean scan finds the fingerprint
again, or when a /register call arrives for the fingerprint.

Distinct from `hidden_at` (operator-controlled presentation/notification
suppression). Both columns can be set on the same row simultaneously.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("crons")}
    if "soft_deleted_at" not in existing_cols:
        with op.batch_alter_table("crons") as batch_op:
            batch_op.add_column(sa.Column("soft_deleted_at", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("crons")}
    if "soft_deleted_at" in existing_cols:
        with op.batch_alter_table("crons") as batch_op:
            batch_op.drop_column("soft_deleted_at")
