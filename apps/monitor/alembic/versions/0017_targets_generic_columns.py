"""STAGE-003-004: add generic columns to `targets` for multi-kind targets.

Adds: kind, status, first_seen, last_seen, hidden_at, labels (JSON), source.
Creates idx_targets_kind for the API filter "WHERE kind = 'docker_container'".

This migration MUST run BEFORE 0018 (which creates targets_docker
referencing targets.id via FK CASCADE).

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = {c["name"] for c in inspector.get_columns("targets")}
    with op.batch_alter_table("targets") as batch_op:
        if "kind" not in existing:
            batch_op.add_column(
                sa.Column("kind", sa.Text(), nullable=False, server_default="unknown")
            )
        if "status" not in existing:
            batch_op.add_column(sa.Column("status", sa.Text(), nullable=True))
        if "first_seen" not in existing:
            batch_op.add_column(sa.Column("first_seen", sa.Text(), nullable=True))
        if "last_seen" not in existing:
            batch_op.add_column(sa.Column("last_seen", sa.Text(), nullable=True))
        if "hidden_at" not in existing:
            batch_op.add_column(sa.Column("hidden_at", sa.Text(), nullable=True))
        if "labels" not in existing:
            batch_op.add_column(sa.Column("labels", sa.Text(), nullable=True))
        if "source" not in existing:
            batch_op.add_column(sa.Column("source", sa.Text(), nullable=True))

    existing_indexes = {i["name"] for i in inspector.get_indexes("targets")}
    if "idx_targets_kind" not in existing_indexes:
        op.create_index("idx_targets_kind", "targets", ["kind"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_indexes = {i["name"] for i in inspector.get_indexes("targets")}
    if "idx_targets_kind" in existing_indexes:
        op.drop_index("idx_targets_kind", table_name="targets")
    existing = {c["name"] for c in inspector.get_columns("targets")}
    with op.batch_alter_table("targets") as batch_op:
        for col in ("source", "labels", "hidden_at", "last_seen", "first_seen", "status", "kind"):
            if col in existing:
                batch_op.drop_column(col)
