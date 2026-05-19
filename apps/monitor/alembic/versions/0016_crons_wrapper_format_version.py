"""STAGE-002-012: add crons.wrapper_format_version column.

Records the WRAPPER_FORMAT_VERSION (a semver string) of the wrapper installed
for this cron. NULL = either no wrapper installed, OR a pre-run-log
baked-fingerprint wrapper (which recorded no version) — both treated as
"outdated" by _compute_wrapper_health. Set at install time.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("crons")}
    if "wrapper_format_version" not in existing_cols:
        with op.batch_alter_table("crons") as batch_op:
            batch_op.add_column(sa.Column("wrapper_format_version", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("crons")}
    if "wrapper_format_version" in existing_cols:
        with op.batch_alter_table("crons") as batch_op:
            batch_op.drop_column("wrapper_format_version")
