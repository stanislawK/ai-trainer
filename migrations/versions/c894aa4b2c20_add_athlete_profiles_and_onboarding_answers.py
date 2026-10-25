"""add athlete_profiles and onboarding_answers

Expand-only (ADR-0024): two new user-owned tables for AI onboarding (ADR-0028, ticket #254). No
existing table or row changes. Integers are `bigint` and strings `text`, each bounded by a CHECK,
so squawk's default rules pass (ADR-0024).

Revision ID: c894aa4b2c20
Revises: d3a7e5c1b942
Create Date: 2026-10-24 23:30:05.054907

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c894aa4b2c20"
down_revision: str | Sequence[str] | None = "d3a7e5c1b942"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "athlete_profiles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("birth_year", sa.BigInteger(), nullable=True),
        sa.Column("height_cm", sa.BigInteger(), nullable=True),
        sa.Column("weight_kg", sa.Numeric(precision=4, scale=1), nullable=True),
        sa.Column("limits", sa.Text(), nullable=True),
        sa.CheckConstraint("height_cm BETWEEN 100 AND 250", name="athlete_profiles_height_range"),
        sa.CheckConstraint("weight_kg BETWEEN 25 AND 300", name="athlete_profiles_weight_range"),
        sa.CheckConstraint(
            "birth_year BETWEEN 1900 AND 2100", name="athlete_profiles_birth_year_range"
        ),
        sa.CheckConstraint("char_length(limits) <= 300", name="athlete_profiles_limits_length"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "onboarding_answers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("sport_id", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("parsed", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(step = 'about_you') = (sport_id IS NULL)", name="onboarding_answers_sport_per_step"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'parsed', 'failed', 'skipped')",
            name="onboarding_answers_status",
        ),
        sa.CheckConstraint(
            "step IN ('about_you', 'goals', 'level', 'schedule')", name="onboarding_answers_step"
        ),
        sa.CheckConstraint("revision >= 1", name="onboarding_answers_revision_positive"),
        sa.CheckConstraint("char_length(text) <= 1000", name="onboarding_answers_text_length"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Its first column indexes `user_id` too.
        sa.UniqueConstraint(
            "user_id",
            "step",
            "sport_id",
            name="onboarding_answers_user_step_sport_key",
            postgresql_nulls_not_distinct=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("onboarding_answers")
    op.drop_table("athlete_profiles")
