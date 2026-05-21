"""STAGE-003-004: targets_docker sidecar table.

Per T-TARGETS-SCHEMA: Docker-specific columns kept off the generic `targets`
row so other target kinds (hosts, network gear, etc.) don't carry NULL Docker
columns. FK CASCADE deletes the sidecar row when the parent target is deleted.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "targets_docker" in set(inspector.get_table_names()):
        return  # idempotent
    op.create_table(
        "targets_docker",
        sa.Column("target_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("restart_count", sa.Integer(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("healthcheck", sa.Text(), nullable=True),
        sa.Column("image", sa.Text(), nullable=True),
        sa.Column("network_mode", sa.Text(), nullable=True),
        sa.Column("cpu_pct_cached", sa.Float(), nullable=True),
        sa.Column("mem_mib_cached", sa.Float(), nullable=True),
        sa.Column("metrics_cached_at", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["targets.id"],
            ondelete="CASCADE",
            name="fk_targets_docker_target_id",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "targets_docker" not in set(inspector.get_table_names()):
        return
    op.drop_table("targets_docker")
