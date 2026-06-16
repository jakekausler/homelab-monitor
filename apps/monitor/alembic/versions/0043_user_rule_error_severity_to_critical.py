"""Remap log_user_rules severity 'error' -> 'critical'.

'error' is no longer a valid alert severity (valid vocab: info|warning|critical).
Any pre-existing user rule stored with severity='error' is bumped to 'critical'
(worst-case-to-critical), so reads no longer fail the Pydantic Literal /
VALID_SEVERITIES validation.

Revision ID: 0043
Revises: 0042
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE log_user_rules SET severity = 'critical' WHERE severity = 'error'"))


def downgrade() -> None:
    # One-way migration: the original 'error' rows are indistinguishable from
    # genuinely-'critical' rows after upgrade, so downgrade cannot restore them.
    # No-op (intentional).
    pass
