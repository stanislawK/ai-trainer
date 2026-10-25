"""add sport_profiles

Expand-only (ADR-0024): one new user-owned table for the sport part of the onboarding profile
(ADR-0028, ticket #255). No existing table or row changes. Integers are `bigint` and strings
`text`, each bounded by a CHECK, so squawk's default rules pass (ADR-0024); the preferred days
are a `bigint[]` of weekdays, 0 for Monday.

Revision ID: 7d2c9e4a1f63
Revises: c894aa4b2c20
Create Date: 2026-10-25 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "7d2c9e4a1f63"
down_revision: str | Sequence[str] | None = "c894aa4b2c20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sport_profiles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("sport_id", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), nullable=True),
        sa.Column("exercises", sa.Text(), nullable=True),
        sa.Column("sessions_per_week", sa.BigInteger(), nullable=True),
        sa.Column("session_minutes", sa.BigInteger(), nullable=True),
        sa.Column("preferred_days", postgresql.ARRAY(sa.BigInteger()), nullable=True),
        sa.CheckConstraint("char_length(level) <= 200", name="sport_profiles_level_length"),
        sa.CheckConstraint("char_length(exercises) <= 300", name="sport_profiles_exercises_length"),
        sa.CheckConstraint(
            "sessions_per_week BETWEEN 1 AND 14", name="sport_profiles_sessions_range"
        ),
        sa.CheckConstraint(
            "session_minutes BETWEEN 5 AND 600", name="sport_profiles_minutes_range"
        ),
        sa.CheckConstraint(
            "preferred_days <@ ARRAY[0, 1, 2, 3, 4, 5, 6]::bigint[]",
            name="sport_profiles_days_range",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        # Its first column indexes `user_id` too.
        sa.PrimaryKeyConstraint("user_id", "sport_id"),
    )


def downgrade() -> None:
    op.drop_table("sport_profiles")
