"""STAGE-002-005: rename ``crons.wrapper_installed_at`` → ``crons.wrapper_last_seen_at``.

Non-destructive column rename. The legacy column ``wrapper_installed_at`` was a
single-shot install timestamp; under the locked design (STAGE-002-005 D2/D10) the
column is now refreshed on every wrapper-mode ``/register`` call, so the semantic
is "last seen", not "installed". The rename eliminates ambiguity.

SQLite supports ``RENAME COLUMN`` natively (3.25+); ``batch_alter_table`` is used
here for consistency with the rest of this codebase's ALTER pattern (cf. 0006,
0007). On a fresh DB the rename is instantaneous; on a dev DB with seed rows the
column value is preserved verbatim.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("crons") as batch_op:
        batch_op.alter_column(
            "wrapper_installed_at",
            new_column_name="wrapper_last_seen_at",
        )


def downgrade() -> None:
    with op.batch_alter_table("crons") as batch_op:
        batch_op.alter_column(
            "wrapper_last_seen_at",
            new_column_name="wrapper_installed_at",
        )
