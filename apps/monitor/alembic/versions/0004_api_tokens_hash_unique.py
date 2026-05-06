"""STAGE-001-011: Add UNIQUE index on api_tokens.hash for O(log n) lookup.

The auth subsystem looks up API tokens by SHA-256(plaintext); a UNIQUE index
both speeds the common path AND prevents two rows hashing to the same value
from a partial-transaction retry.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "api_tokens_hash_idx",
        "api_tokens",
        ["hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("api_tokens_hash_idx", table_name="api_tokens")
