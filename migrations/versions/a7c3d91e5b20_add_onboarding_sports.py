"""add users.onboarded_at and user_sports

Revision ID: a7c3d91e5b20
Revises: f0ddfe1662db
Create Date: 2026-09-29 14:02:11.482913

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3d91e5b20"
down_revision: str | Sequence[str] | None = "f0ddfe1662db"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("onboarded_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.create_table(
        "user_sports",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("sport_id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "sport_id"),
    )


def downgrade() -> None:
    op.drop_table("user_sports")
    op.drop_column("users", "onboarded_at")
