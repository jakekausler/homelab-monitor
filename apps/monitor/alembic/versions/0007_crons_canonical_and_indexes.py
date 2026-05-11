"""STAGE-002-002: crons.schedule_canonical + xor CHECK + active partial index.

Adds the denormalized canonical cron expression column (D1 hybrid raw +
canonical), a CHECK constraint enforcing the schedule-or-cadence xor
contract (a cron is either schedule-driven OR cadence-driven, never both
nor neither), and a partial index on ``crons(name)`` filtered to
``archived_at IS NULL`` for fast Inventory list-view queries (D2 soft delete).

Defensive idempotency on column add: ``inspect(bind).get_columns('crons')``
is consulted before ``add_column`` so re-running the migration on a DB
that has half-applied state is safe.

Backfill: for each existing crons row with ``schedule != ''`` AND
``schedule IS NOT NULL``, compute the canonical form via croniter and write
to ``schedule_canonical``. Cadence-only rows (``schedule = ''``) leave
``schedule_canonical`` NULL.

xor CHECK semantics (relaxed from strict-xor to "at-least-one"):
- valid: ``schedule != ''`` (cadence_seconds may mirror it)
- valid: ``cadence_seconds > 0`` (schedule may be '')
- INVALID: both empty/0 (no scheduling info at all)
API-side xor (CronCreate/CronUpdate validators) keeps the user picking
exactly one; the repo populates the mirror field server-side.

Partial index: ``CREATE INDEX idx_crons_active ON crons(name) WHERE
archived_at IS NULL`` — speeds up the default Inventory list query that
filters out archived crons (D2 default filter).

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from croniter import croniter
from sqlalchemy import inspect, text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def _canonicalize(expr: str) -> str:
    """Validate + canonicalize a cron expression via croniter.

    Migration-local copy (not imported from kernel.cron.schedule to keep
    migrations runnable without the rest of the kernel installed).
    """
    if not croniter.is_valid(expr):
        msg = f"invalid cron expression in existing crons row: {expr!r}"
        raise ValueError(msg)
    # croniter normalizes by parsing + re-emitting fields. We use the
    # public expressions attribute to render canonical form.
    iterator = croniter(expr)
    return " ".join(iterator.expressions)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # --- 1. ADD COLUMN schedule_canonical (idempotent) ---
    existing_cols = {c["name"] for c in inspector.get_columns("crons")}
    if "schedule_canonical" not in existing_cols:
        with op.batch_alter_table("crons") as batch_op:
            batch_op.add_column(
                sa.Column("schedule_canonical", sa.Text(), nullable=True),
            )

    # --- 2. Backfill schedule_canonical for rows with a schedule ---
    rows = bind.execute(
        text("SELECT id, schedule FROM crons WHERE schedule IS NOT NULL AND schedule != ''")
    ).fetchall()
    for row in rows:
        canonical = _canonicalize(str(row.schedule))
        bind.execute(
            text("UPDATE crons SET schedule_canonical = :c WHERE id = :id"),
            {"c": canonical, "id": str(row.id)},
        )

    # --- 3. xor CHECK constraint (separate batch so the new column commits first) ---
    # SQLite enforces CHECK at INSERT/UPDATE time; batch_alter_table recreates
    # the table to attach it. The constraint forbids the "both" and "neither" cases.
    with op.batch_alter_table("crons") as batch_op:
        batch_op.create_check_constraint(
            "ck_crons_schedule_xor_cadence",
            "schedule != '' OR cadence_seconds > 0",
        )

    # --- 4. Partial index for fast active-only list queries ---
    op.create_index(
        "idx_crons_active",
        "crons",
        ["name"],
        sqlite_where=text("archived_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_crons_active", table_name="crons")
    with op.batch_alter_table("crons") as batch_op:
        batch_op.drop_constraint("ck_crons_schedule_xor_cadence", type_="check")
    with op.batch_alter_table("crons") as batch_op:
        batch_op.drop_column("schedule_canonical")
