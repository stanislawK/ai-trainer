"""add exercise catalogue

Expand-only (ADR-0024): two extensions, one function and three new tables for the exercise
catalogue (ADR-0031, ticket #375). No existing table or row changes, and this revision writes no
rows; `ai-trainer import-exercises` does. Athletes' custom exercises and aliases cascade with
their account (G7).

`unaccent` is only STABLE, so an index can't call it. `exercise_name_norm` wraps it with an
explicit dictionary, as IMMUTABLE, and both the name index and the generated `alias_norm` use it.

Revision ID: b6f2a8d41c37
Revises: e8b1f4c7a2d9
Create Date: 2026-10-31 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b6f2a8d41c37"
down_revision: str | Sequence[str] | None = "e8b1f4c7a2d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION exercise_name_norm(value text) RETURNS text
        LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
        RETURN btrim(
            regexp_replace(
                lower(public.unaccent('public.unaccent'::regdictionary, value)), '\\s+', ' ', 'g'
            )
        )
        """
    )
    op.create_table(
        "exercises",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sport_id", sa.Text(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("equipment", sa.Text(), nullable=True),
        sa.Column("body_part", sa.Text(), nullable=True),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("secondary_muscles", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("default_measure", sa.Text(), nullable=False),
        sa.Column("attribution", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "source <> 'custom' OR owner_user_id IS NOT NULL", name="exercises_owner"
        ),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 200", name="exercises_name_length"),
        sa.CheckConstraint(
            "default_measure IN ('load_reps', 'reps', 'time', 'distance', 'band_reps')",
            name="exercises_default_measure",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "source_ref", name="exercises_source_source_ref_key"),
    )
    op.create_index("ix_exercises_owner_user_id", "exercises", ["owner_user_id"])
    op.create_index(
        "ix_exercises_name_trgm",
        "exercises",
        [sa.text("exercise_name_norm(name) gin_trgm_ops")],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_exercises_name_tsv",
        "exercises",
        [sa.text("to_tsvector('simple', name)")],
        postgresql_using="gin",
    )
    op.create_table(
        "exercise_texts",
        sa.Column("exercise_id", sa.Uuid(), nullable=False),
        sa.Column("lang", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("steps", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["exercise_id"], ["exercises.id"], ondelete="CASCADE"),
        # Its first column indexes `exercise_id` too.
        sa.PrimaryKeyConstraint("exercise_id", "lang"),
    )
    op.create_table(
        "exercise_aliases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("exercise_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column(
            "alias_norm",
            sa.Text(),
            sa.Computed("exercise_name_norm(alias)", persisted=True),
            nullable=False,
        ),
        sa.Column("lang", sa.Text(), nullable=True),
        sa.CheckConstraint("char_length(alias) BETWEEN 1 AND 200", name="exercise_aliases_length"),
        sa.ForeignKeyConstraint(["exercise_id"], ["exercises.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_exercise_aliases_exercise_id", "exercise_aliases", ["exercise_id"])
    # Also indexes `user_id` for the account deletion's cascade.
    op.create_index(
        "exercise_aliases_user_alias_key",
        "exercise_aliases",
        ["user_id", "alias_norm"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index(
        "exercise_aliases_global_alias_key",
        "exercise_aliases",
        ["exercise_id", "alias_norm"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )
    op.create_index(
        "ix_exercise_aliases_alias_norm_trgm",
        "exercise_aliases",
        [sa.text("alias_norm gin_trgm_ops")],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_exercise_aliases_alias_tsv",
        "exercise_aliases",
        [sa.text("to_tsvector('simple', alias)")],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_table("exercise_aliases")
    op.drop_table("exercise_texts")
    op.drop_table("exercises")
    op.execute("DROP FUNCTION IF EXISTS exercise_name_norm(text)")
    op.execute("DROP EXTENSION IF EXISTS unaccent")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
