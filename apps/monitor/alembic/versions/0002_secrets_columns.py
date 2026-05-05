"""secrets columns: ciphertext, kdf_salt, rotated_at

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-05

Stage: STAGE-001-005 (encrypted secrets store).

Adds three columns to the existing ``secrets`` stub created in 0001:
- ``ciphertext TEXT NOT NULL`` — base64-encoded ``nonce||aead_payload`` blob
- ``kdf_salt BLOB NOT NULL`` — 16 random bytes per row, fed to HKDF
- ``rotated_at TEXT NULL`` — ISO-8601 UTC; NULL until first rotation

The stub is empty in any fresh DB (no rows have been inserted prior to this
migration in any deployment), so ``NOT NULL`` columns can be added without a
backfill. SQLite ALTER TABLE ADD COLUMN supports ``NOT NULL`` only when the
column is also declared with a default OR when the table has zero rows; we
rely on the latter.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    row_count = conn.execute(sa.text("SELECT COUNT(*) FROM secrets")).scalar()
    if row_count and row_count > 0:
        raise RuntimeError(
            f"refusing to upgrade 0002: secrets table has {row_count} row(s); "
            "the NOT NULL columns cannot be added without a backfill. "
            "Drop or migrate existing rows manually first."
        )
    op.add_column("secrets", sa.Column("ciphertext", sa.Text(), nullable=False))
    op.add_column("secrets", sa.Column("kdf_salt", sa.LargeBinary(), nullable=False))
    op.add_column("secrets", sa.Column("rotated_at", sa.Text(), nullable=True))
    op.create_index("ix_secrets_name_unique", "secrets", ["name"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_secrets_name_unique", table_name="secrets")
    op.drop_column("secrets", "rotated_at")
    op.drop_column("secrets", "kdf_salt")
    op.drop_column("secrets", "ciphertext")
