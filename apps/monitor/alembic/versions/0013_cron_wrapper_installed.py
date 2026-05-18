"""STAGE-002-009A: add crons.wrapper_installed column.

Boolean column indicating whether the cron entry is currently wrapped. Populated
from the crontab parser (signals whether the on-disk line invokes the wrapper),
and updated by install/uninstall operations.

Distinct from wrapper_last_seen_at (health signal). This column gates the
Install/Remove UI toggle state.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("crons")}
    if "wrapper_installed" not in existing_cols:
        with op.batch_alter_table("crons") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "wrapper_installed", sa.Boolean(), nullable=False, server_default=sa.text("0")
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_cols = {c["name"] for c in inspector.get_columns("crons")}
    if "wrapper_installed" in existing_cols:
        with op.batch_alter_table("crons") as batch_op:
            batch_op.drop_column("wrapper_installed")
