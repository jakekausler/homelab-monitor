"""STAGE-002-007: add crons.last_discovered_at column.

UTC ISO-8601 timestamp; bumped on every discovery scan that sees the fingerprint.
NULL means the cron has never been seen by the discovery scanner (typical for
crons registered via wrapper-mode /register from remote hosts, or any cron whose
source file is outside the monitor's bind-mounted /host tree).

This column does NOT signal "stale" or "deleted" by itself. STAGE-002-007A
introduces a separate `soft_deleted_at` column for that purpose, populated by
the discoverer's reconciliation logic when a scan completes successfully without
seeing a previously-known fingerprint.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("crons")}
    if "last_discovered_at" not in existing_cols:
        with op.batch_alter_table("crons") as batch_op:
            batch_op.add_column(sa.Column("last_discovered_at", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("crons")}
    if "last_discovered_at" in existing_cols:
        with op.batch_alter_table("crons") as batch_op:
            batch_op.drop_column("last_discovered_at")
