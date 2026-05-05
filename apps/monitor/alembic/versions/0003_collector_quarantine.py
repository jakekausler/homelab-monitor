"""STAGE-001-008: Add collector quarantine state + unique name index.

Adds three columns to ``collectors`` for failure-budget quarantine state:

- ``quarantined_at``: ISO-8601 UTC timestamp; NULL when not quarantined.
- ``quarantine_reason``: text; NULL when not quarantined.
- ``consecutive_failures``: integer; default 0; updated by FailureBudget.

Adds a unique index on ``collectors.name``. The FailureBudget keys lookups
by name; uniqueness is an invariant the scheduler+loader already assume,
this migration enforces it. Defensively checks for existing duplicate names
before adding the index; raises if any are found (pre-1.0, no expected
production data).

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    if not context.is_offline_mode():
        # Defensive duplicate-name guard: must hold before adding unique index.
        # Only runs in online mode (live DB connection); offline mode generates
        # SQL scripts without executing them, so we skip this check.
        bind = op.get_bind()
        duplicates = bind.execute(
            sa.text("SELECT name, COUNT(*) AS c FROM collectors GROUP BY name HAVING c > 1")
        ).fetchall()
        if duplicates:
            names = ", ".join(repr(row[0]) for row in duplicates)
            msg = (
                f"Cannot add unique index on collectors.name: duplicate names found "
                f"({names}). Resolve duplicates before running this migration."
            )
            raise RuntimeError(msg)

    op.add_column(
        "collectors",
        sa.Column("quarantined_at", sa.Text(), nullable=True),
    )
    op.add_column(
        "collectors",
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "collectors",
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_index(
        "ix_collectors_name_unique",
        "collectors",
        ["name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_collectors_name_unique", table_name="collectors")
    op.drop_column("collectors", "consecutive_failures")
    op.drop_column("collectors", "quarantine_reason")
    op.drop_column("collectors", "quarantined_at")
