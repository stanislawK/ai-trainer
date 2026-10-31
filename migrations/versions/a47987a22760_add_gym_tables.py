"""add gym tables

The gym's own tables of ADR-0032 (ticket #376): `gym_exercise_entries`, one row per exercise in
a session, `gym_sets`, one row per set, and `athlete_bands`. Each is user-owned and every
reference to a user-owned row is keyed with `user_id`, so no row can point at another user's
data, and account deletion empties all three (G7).

Expand only (ADR-0024): three new tables and one unique constraint on `training_session_sources`
that its primary key already guarantees, the target of an entry's `source_id`. No existing row
is touched and `training_sessions.payload` is never rewritten (ADR-0022 invariant 4); existing
gym sessions get their rows from `ai-trainer backfill-gym-sessions` (#377).

Revision ID: a47987a22760
Revises: b6f2a8d41c37
Create Date: 2026-10-31 13:09:08.948010

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a47987a22760"
down_revision: str | Sequence[str] | None = "b6f2a8d41c37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# squawk-ignore prefer-bigint-over-smallint, prefer-bigint-over-int: ADR-0032 types (#376)
# ADR-0032 sets smallint and integer for bounded measures (position, set_no, reps, RIR,
# implements, rank, duration_s), never for IDs; the lock rules stay on.

_SOURCE_KEY = "training_session_sources_id_user_id_key"


def upgrade() -> None:
    # The target of the composite `source_id` key below; `id` alone is already unique. Built
    # concurrently, then attached, so the existing table is never locked for the build.
    with op.get_context().autocommit_block():
        op.create_index(
            _SOURCE_KEY,
            "training_session_sources",
            ["id", "user_id"],
            unique=True,
            postgresql_concurrently=True,
            if_not_exists=True,
        )
    op.execute(
        f"ALTER TABLE training_session_sources ADD CONSTRAINT {_SOURCE_KEY}"
        f" UNIQUE USING INDEX {_SOURCE_KEY}"
    )
    op.create_table(
        "athlete_bands",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column(
            "label_norm",
            sa.Text(),
            sa.Computed("exercise_name_norm(label)", persisted=True),
            nullable=False,
        ),
        sa.Column("rank", sa.SmallInteger(), nullable=True),
        sa.Column("nominal_kg", sa.Numeric(precision=5, scale=1), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "user_id", name="athlete_bands_id_user_id_key"),
        sa.UniqueConstraint("user_id", "label_norm", name="athlete_bands_user_id_label_norm_key"),
    )
    op.create_index("ix_athlete_bands_user_id", "athlete_bands", ["user_id"])
    op.create_table(
        "gym_exercise_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("exercise_id", sa.Uuid(), nullable=True),
        sa.Column("name_as_typed", sa.Text(), nullable=False),
        sa.Column("measure", sa.Text(), nullable=False),
        sa.Column("per_side", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("implements", sa.SmallInteger(), nullable=True),
        sa.Column("rir", sa.SmallInteger(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "measure IN ('load_reps', 'reps', 'time', 'distance', 'band_reps')",
            name="gym_exercise_entries_measure",
        ),
        sa.CheckConstraint(
            "implements BETWEEN 1 AND 4", name="gym_exercise_entries_implements_range"
        ),
        sa.CheckConstraint("rir BETWEEN 0 AND 10", name="gym_exercise_entries_rir_range"),
        sa.CheckConstraint("char_length(note) <= 500", name="gym_exercise_entries_note_length"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id", "user_id"],
            ["training_sessions.id", "training_sessions.user_id"],
            name="gym_exercise_entries_session_id_user_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "user_id"],
            ["training_session_sources.id", "training_session_sources.user_id"],
            name="gym_exercise_entries_source_id_user_id_fkey",
            ondelete="CASCADE",
        ),
        # No ON DELETE action, owner-chosen at #376's plan gate.
        sa.ForeignKeyConstraint(["exercise_id"], ["exercises.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "user_id", name="gym_exercise_entries_id_user_id_key"),
        sa.UniqueConstraint(
            "session_id", "position", name="gym_exercise_entries_session_id_position_key"
        ),
    )
    op.create_index(
        "ix_gym_exercise_entries_user_id_exercise_id_session_id",
        "gym_exercise_entries",
        ["user_id", "exercise_id", "session_id"],
    )
    op.create_index("ix_gym_exercise_entries_source_id", "gym_exercise_entries", ["source_id"])
    op.create_index("ix_gym_exercise_entries_exercise_id", "gym_exercise_entries", ["exercise_id"])
    op.create_table(
        "gym_sets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("entry_id", sa.Uuid(), nullable=False),
        sa.Column("set_no", sa.SmallInteger(), nullable=False),
        sa.Column("reps", sa.SmallInteger(), nullable=True),
        sa.Column("load_kg", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("load_original", sa.Text(), nullable=True),
        sa.Column("duration_s", sa.Integer(), nullable=True),
        sa.Column("distance_m", sa.Numeric(precision=8, scale=1), nullable=True),
        sa.Column("band_id", sa.Uuid(), nullable=True),
        sa.Column("band_label", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["entry_id", "user_id"],
            ["gym_exercise_entries.id", "gym_exercise_entries.user_id"],
            name="gym_sets_entry_id_user_id_fkey",
            ondelete="CASCADE",
        ),
        # SET NULL clears only the band column: `user_id` is NOT NULL and stays.
        sa.ForeignKeyConstraint(
            ["band_id", "user_id"],
            ["athlete_bands.id", "athlete_bands.user_id"],
            name="gym_sets_band_id_user_id_fkey",
            ondelete="SET NULL (band_id)",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entry_id", "set_no", name="gym_sets_entry_id_set_no_key"),
    )
    op.create_index("ix_gym_sets_user_id", "gym_sets", ["user_id"])
    op.create_index("ix_gym_sets_band_id", "gym_sets", ["band_id"])


def downgrade() -> None:
    op.drop_table("gym_sets")
    op.drop_table("gym_exercise_entries")
    op.drop_table("athlete_bands")
    op.drop_constraint(_SOURCE_KEY, "training_session_sources", type_="unique")
