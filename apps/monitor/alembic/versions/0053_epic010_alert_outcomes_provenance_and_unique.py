"""EPIC-010 alert_outcomes provenance + unique index.

Revision ID: 0053
Revises: 0052
Create Date: 2026-07-06T00:00:00

Widens ``alert_outcomes.decided_by`` from ``INTEGER FK users.id`` to ``TEXT``
so the STAGE-010-003 Karma-outcome reconciler can record provenance strings
(``"karma"``, ``"reconciler"``, and, in a future stage, ``"backfill"``).
Human-ack callers coerce ``user.id`` to a string.

Adds a UNIQUE index on ``(alert_id, outcome)`` so
``AlertRepository.insert_outcome_if_absent`` can rely on
``INSERT ... ON CONFLICT (alert_id, outcome) DO NOTHING`` for
idempotent hourly ticks.

Downgrade is best-effort: ``ALTER COLUMN`` back to INTEGER will FAIL if
any row's ``decided_by`` is non-numeric (which is precisely the case in
production after this stage lands). Documented, not fixed.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Drop the existing FK (created with explicit name in 0005) + widen the
    # column type. SQLite requires batch mode for both operations.
    with op.batch_alter_table("alert_outcomes") as batch_op:
        batch_op.drop_constraint(
            "fk_alert_outcomes_decided_by_users",
            type_="foreignkey",
        )
        batch_op.alter_column(
            "decided_by",
            existing_type=sa.Integer(),
            type_=sa.Text(),
            existing_nullable=True,
        )

    # UNIQUE on (alert_id, outcome) so INSERT ... ON CONFLICT DO NOTHING works.
    op.create_index(
        "uq_alert_outcomes_alert_id_outcome",
        "alert_outcomes",
        ["alert_id", "outcome"],
        unique=True,
    )


def downgrade() -> None:
    # BEST-EFFORT: the ALTER back to INTEGER raises if any decided_by value is
    # non-numeric (which is the whole point of upgrading — reconciler writes
    # ``"karma"`` / ``"reconciler"``). Downgrade should only be attempted
    # against a fresh / test DB.
    op.drop_index(
        "uq_alert_outcomes_alert_id_outcome",
        table_name="alert_outcomes",
    )
    with op.batch_alter_table("alert_outcomes") as batch_op:
        batch_op.alter_column(
            "decided_by",
            existing_type=sa.Text(),
            type_=sa.Integer(),
            existing_nullable=True,
        )
        batch_op.create_foreign_key(
            "fk_alert_outcomes_decided_by_users",
            "users",
            ["decided_by"],
            ["id"],
        )
