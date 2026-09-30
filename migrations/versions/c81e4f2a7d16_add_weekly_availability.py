"""add weekly_availability

Revision ID: c81e4f2a7d16
Revises: a7c3d91e5b20
Create Date: 2026-09-30 09:12:45.301774

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c81e4f2a7d16"
down_revision: str | Sequence[str] | None = "a7c3d91e5b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "weekly_availability",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("minutes", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("minutes BETWEEN 1 AND 600", name="weekly_availability_minutes_range"),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="weekly_availability_weekday_range"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "weekday"),
    )


def downgrade() -> None:
    op.drop_table("weekly_availability")
