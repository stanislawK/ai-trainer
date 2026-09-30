"""add goal sport

Revision ID: 3b8e5d4f7a91
Revises: 1959a40c6d92
Create Date: 2026-09-30 14:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3b8e5d4f7a91"
down_revision: str | Sequence[str] | None = "1959a40c6d92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no backfill: goals stored before this stay general (ADR-0006).
    op.add_column("goals", sa.Column("sport_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("goals", "sport_id")
