"""add user timezone

Revision ID: 5d2f8a1c9e47
Revises: 3b8e5d4f7a91
Create Date: 2026-09-30 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5d2f8a1c9e47"
down_revision: str | Sequence[str] | None = "3b8e5d4f7a91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # An IANA name; every existing user starts on UTC until they confirm one (ADR-0014).
    op.add_column("users", sa.Column("timezone", sa.String(), server_default="UTC", nullable=False))


def downgrade() -> None:
    op.drop_column("users", "timezone")
