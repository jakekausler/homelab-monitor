"""STAGE-002-001: heartbeat receiver schema (crons + heartbeats_state).

Expands the SCAFFOLDING-stage tables created in 0001_initial_schema.py to their
spec §6.1 shape:

- ``crons``: name, host, schedule, cadence_seconds, expected_grace_seconds,
  integration_mode (CHECK observe/heartbeat/both), enabled, last_seen_state
  (CHECK unknown/running/ok/failed/late), updated_at, archived_at.
  ``id`` and ``command`` and ``created_at`` already exist from 0001.
- ``heartbeats_state``: REPLACED. The 0001 stub had ``id PRIMARY KEY`` + a
  meaningless ``key`` column; the real table is keyed by ``cron_id`` (FK to
  crons.id ON DELETE CASCADE) and tracks current_state, last_start_at,
  last_ok_at, last_fail_at, current_streak, expected_next_at,
  last_duration_seconds, last_exit_code, updated_at.

Defensive idempotency on ``crons``: ``inspect(bind).get_columns('crons')``
is consulted before each ``add_column`` so re-running the migration on a DB
that has half-applied state (e.g. a partially-failed earlier run) is safe.

For ``heartbeats_state`` we DROP and CREATE; the stub is empty (the SCAFFOLDING
docstring in 0001 explicitly says the columns added later are by additive
migration, and no production code has inserted rows into the stub).

Indexes:
- ``ix_crons_integration_mode_enabled`` on ``crons(integration_mode, enabled)``
  for fast scan of "which crons are subject to heartbeat enforcement?"

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


# Columns to add to ``crons`` (in order). Each tuple: (name, sa.Column factory).
# The factory is invoked at apply time so each ADD COLUMN gets a fresh Column
# (SQLAlchemy ``Column`` instances cannot be reused across operations).
def _crons_columns_to_add() -> list[tuple[str, sa.Column[object]]]:
    return [
        ("name", sa.Column("name", sa.Text(), nullable=False, server_default="")),
        ("host", sa.Column("host", sa.Text(), nullable=False, server_default="")),
        ("schedule", sa.Column("schedule", sa.Text(), nullable=False, server_default="")),
        (
            "cadence_seconds",
            sa.Column("cadence_seconds", sa.Integer(), nullable=False, server_default="0"),
        ),
        (
            "expected_grace_seconds",
            sa.Column(
                "expected_grace_seconds",
                sa.Integer(),
                nullable=False,
                server_default="300",
            ),
        ),
        (
            "integration_mode",
            sa.Column(
                "integration_mode",
                sa.Text(),
                nullable=False,
                server_default="observe",
            ),
        ),
        (
            "enabled",
            sa.Column("enabled", sa.Integer(), nullable=False, server_default="1"),
        ),
        (
            "last_seen_state",
            sa.Column(
                "last_seen_state",
                sa.Text(),
                nullable=False,
                server_default="unknown",
            ),
        ),
        (
            "updated_at",
            sa.Column("updated_at", sa.Text(), nullable=False, server_default=""),
        ),
        ("archived_at", sa.Column("archived_at", sa.Text(), nullable=True)),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # ----- crons: add columns idempotently via batch_alter_table -----
    existing_cols = {c["name"] for c in inspector.get_columns("crons")}
    cols_to_add = [
        (name, factory) for name, factory in _crons_columns_to_add() if name not in existing_cols
    ]
    if cols_to_add:
        with op.batch_alter_table("crons") as batch_op:
            for _name, col in cols_to_add:
                batch_op.add_column(col)

    # CHECK constraints + named index added in a separate batch so the new
    # columns from above are committed before the CHECK is evaluated.
    # SQLite cannot ADD CONSTRAINT; the CHECK is enforced via the recreate
    # path that batch_alter_table uses internally when given create_check_constraint.
    with op.batch_alter_table("crons") as batch_op:
        batch_op.create_check_constraint(
            "ck_crons_integration_mode",
            "integration_mode IN ('observe', 'heartbeat', 'both')",
        )
        batch_op.create_check_constraint(
            "ck_crons_last_seen_state",
            "last_seen_state IN ('unknown', 'running', 'ok', 'failed', 'late')",
        )

    op.create_index(
        "ix_crons_integration_mode_enabled",
        "crons",
        ["integration_mode", "enabled"],
    )

    # ----- heartbeats_state: drop + recreate (stub is empty, wrong shape) -----
    op.drop_table("heartbeats_state")
    op.create_table(
        "heartbeats_state",
        sa.Column(
            "cron_id",
            sa.Text(),
            sa.ForeignKey("crons.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "current_state",
            sa.Text(),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("last_start_at", sa.Text(), nullable=True),
        sa.Column("last_ok_at", sa.Text(), nullable=True),
        sa.Column("last_fail_at", sa.Text(), nullable=True),
        sa.Column(
            "current_streak",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("expected_next_at", sa.Text(), nullable=True),
        sa.Column("last_duration_seconds", sa.Float(), nullable=True),
        sa.Column("last_exit_code", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "current_state IN ('unknown', 'running', 'ok', 'failed', 'late')",
            name="ck_heartbeats_state_current_state",
        ),
    )


def downgrade() -> None:
    # Reverse heartbeats_state to its 0001 stub shape.
    op.drop_table("heartbeats_state")
    op.create_table(
        "heartbeats_state",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )

    # Reverse crons additions.
    op.drop_index("ix_crons_integration_mode_enabled", table_name="crons")
    with op.batch_alter_table("crons") as batch_op:
        batch_op.drop_constraint("ck_crons_last_seen_state", type_="check")
        batch_op.drop_constraint("ck_crons_integration_mode", type_="check")
    with op.batch_alter_table("crons") as batch_op:
        for name, _factory in reversed(_crons_columns_to_add()):
            batch_op.drop_column(name)
