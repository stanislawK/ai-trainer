"""The gym's own tables (ADR-0032): one row per exercise in a session, one per set, and the
athlete's resistance bands. Every table is user-owned and every reference is keyed with
`user_id`, so a row can't point at another user's data. Only the gym's `ActivityStore` writes
them."""

import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from ai_trainer.adapters.orm.base import Base


class GymExerciseEntryOrm(Base):
    """One exercise in a session, with the log (`source_id`) that brought it, so undoing that
    log takes only its own entries (B16, B4). `source_id` is null only on backfilled rows."""

    __tablename__ = "gym_exercise_entries"
    __table_args__ = (
        CheckConstraint(
            "measure IN ('load_reps', 'reps', 'time', 'distance', 'band_reps')",
            name="gym_exercise_entries_measure",
        ),
        CheckConstraint("implements BETWEEN 1 AND 4", name="gym_exercise_entries_implements_range"),
        CheckConstraint("rir BETWEEN 0 AND 10", name="gym_exercise_entries_rir_range"),
        CheckConstraint("char_length(note) <= 500", name="gym_exercise_entries_note_length"),
        UniqueConstraint(
            "session_id", "position", name="gym_exercise_entries_session_id_position_key"
        ),
        UniqueConstraint("id", "user_id", name="gym_exercise_entries_id_user_id_key"),
        ForeignKeyConstraint(
            ["session_id", "user_id"],
            ["training_sessions.id", "training_sessions.user_id"],
            name="gym_exercise_entries_session_id_user_id_fkey",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_id", "user_id"],
            ["training_session_sources.id", "training_session_sources.user_id"],
            name="gym_exercise_entries_source_id_user_id_fkey",
            ondelete="CASCADE",
        ),
        # Progress per exercise (B6, F12); it leads with `user_id`, so it indexes that too.
        Index(
            "ix_gym_exercise_entries_user_id_exercise_id_session_id",
            "user_id",
            "exercise_id",
            "session_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # Null while the entry is typed-only (ADR-0031). No ON DELETE action, owner-chosen at
    # #376's plan gate: an account deletion removes the entries and custom exercises together.
    exercise_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("exercises.id"), nullable=True, index=True
    )
    name_as_typed: Mapped[str] = mapped_column(Text, nullable=False)
    measure: Mapped[str] = mapped_column(Text, nullable=False)
    # "12 each side" is 12 reps per side.
    per_side: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # "Two dumbbells, 6 kg each" is 2, and `load_kg` is per implement.
    implements: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    rir: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class AthleteBandOrm(Base):
    """An athlete's resistance band (B39). A colour is never ranked by the app: `rank` comes
    only from the athlete, and `nominal_kg` only when they state it."""

    __tablename__ = "athlete_bands"
    __table_args__ = (
        UniqueConstraint("user_id", "label_norm", name="athlete_bands_user_id_label_norm_key"),
        UniqueConstraint("id", "user_id", name="athlete_bands_id_user_id_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(Text, nullable=False)
    # The same normalization as an exercise name (ADR-0031), so "Czerwona " and "czerwona" match.
    label_norm: Mapped[str] = mapped_column(
        Text, Computed("exercise_name_norm(label)", persisted=True)
    )
    # 1 is the lightest.
    rank: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    nominal_kg: Mapped[Decimal | None] = mapped_column(Numeric(5, 1), nullable=True)


class GymSetOrm(Base):
    """One set: a pyramid ("10, 8, 6") is three rows. A set with every measure empty is
    allowed ("4 sets"); a missing value is never invented (B38)."""

    __tablename__ = "gym_sets"
    __table_args__ = (
        UniqueConstraint("entry_id", "set_no", name="gym_sets_entry_id_set_no_key"),
        ForeignKeyConstraint(
            ["entry_id", "user_id"],
            ["gym_exercise_entries.id", "gym_exercise_entries.user_id"],
            name="gym_sets_entry_id_user_id_fkey",
            ondelete="CASCADE",
        ),
        # SET NULL clears only the band column: `user_id` is NOT NULL and stays.
        ForeignKeyConstraint(
            ["band_id", "user_id"],
            ["athlete_bands.id", "athlete_bands.user_id"],
            name="gym_sets_band_id_user_id_fkey",
            ondelete="SET NULL (band_id)",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entry_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    set_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    reps: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # Code converts to kg; `load_original` keeps the text as typed (G15).
    load_kg: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    load_original: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_s: Mapped[int | None] = mapped_column(nullable=True)
    distance_m: Mapped[Decimal | None] = mapped_column(Numeric(8, 1), nullable=True)
    band_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    band_label: Mapped[str | None] = mapped_column(Text, nullable=True)
