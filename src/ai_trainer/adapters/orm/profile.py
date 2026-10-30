import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import JsonValue
from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ai_trainer.adapters.orm.base import Base


class GoalOrm(Base):
    """One row per goal the athlete set (ADR-0006): free text of 1-200 characters, an optional
    target date and an optional sport. `position` keeps the order they were entered in. M4 plans
    (B9) extend this table rather than replace it."""

    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint("char_length(text) BETWEEN 1 AND 200", name="goals_text_length"),
        UniqueConstraint("user_id", "position", name="goals_user_id_position_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    text: Mapped[str] = mapped_column(String(200), nullable=False)
    # A calendar date, not a moment, so no timezone (ADR-0014 invariant 2 covers timestamps).
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # A `SportRegistry` ID stored as text and checked against `user_sports` in the application
    # layer, never a database enum (ADR-0006); null is a general goal.
    sport_id: Mapped[str | None] = mapped_column(nullable=True)
    # timestamptz, UTC (ADR-0004 invariant 5).
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )


class AthleteProfileOrm(Base):
    """The onboarding part of the athlete profile (ADR-0028), one row per athlete, written only
    by the summary's confirm. Age is stored as `birth_year` so it never goes stale."""

    __tablename__ = "athlete_profiles"
    __table_args__ = (
        CheckConstraint("height_cm BETWEEN 100 AND 250", name="athlete_profiles_height_range"),
        CheckConstraint("weight_kg BETWEEN 25 AND 300", name="athlete_profiles_weight_range"),
        CheckConstraint(
            "birth_year BETWEEN 1900 AND 2100", name="athlete_profiles_birth_year_range"
        ),
        CheckConstraint("char_length(limits) <= 300", name="athlete_profiles_limits_length"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # `bigint` and `text`, each bounded by a CHECK, so squawk's default rules pass (ADR-0024).
    birth_year: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    height_cm: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    limits: Mapped[str | None] = mapped_column(Text, nullable=True)


class SportProfileOrm(Base):
    """The sport part of the onboarding profile (ADR-0028), one row per athlete and sport,
    written only by the summary's confirm. `preferred_days` holds weekdays, 0 for Monday."""

    __tablename__ = "sport_profiles"
    __table_args__ = (
        CheckConstraint("char_length(level) <= 200", name="sport_profiles_level_length"),
        CheckConstraint("char_length(exercises) <= 300", name="sport_profiles_exercises_length"),
        CheckConstraint("sessions_per_week BETWEEN 1 AND 14", name="sport_profiles_sessions_range"),
        CheckConstraint("session_minutes BETWEEN 5 AND 600", name="sport_profiles_minutes_range"),
        CheckConstraint(
            "preferred_days <@ ARRAY[0, 1, 2, 3, 4, 5, 6]::bigint[]",
            name="sport_profiles_days_range",
        ),
    )

    # The key's first column, so it indexes `user_id` too.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # A `SportRegistry` ID stored as text, never a database enum (ADR-0006).
    sport_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # `bigint` and `text`, each bounded by a CHECK, so squawk's default rules pass (ADR-0024).
    level: Mapped[str | None] = mapped_column(Text, nullable=True)
    exercises: Mapped[str | None] = mapped_column(Text, nullable=True)
    sessions_per_week: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    session_minutes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    preferred_days: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger), nullable=True)


class OnboardingAnswerOrm(Base):
    """One onboarding step's prose (ADR-0028), kept until the summary is confirmed. `revision`
    goes up on every send, so a read writes `parsed` only while it still matches. `sport_id` is
    null for About you, and the unique key treats that null as a value."""

    __tablename__ = "onboarding_answers"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "step",
            "sport_id",
            name="onboarding_answers_user_step_sport_key",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "step IN ('about_you', 'goals', 'level', 'schedule')", name="onboarding_answers_step"
        ),
        CheckConstraint(
            "status IN ('pending', 'parsed', 'failed', 'skipped')",
            name="onboarding_answers_status",
        ),
        CheckConstraint(
            "(step = 'about_you') = (sport_id IS NULL)", name="onboarding_answers_sport_per_step"
        ),
        CheckConstraint("revision >= 1", name="onboarding_answers_revision_positive"),
        CheckConstraint("char_length(text) <= 1000", name="onboarding_answers_text_length"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Indexed through the unique key, whose first column it is.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    step: Mapped[str] = mapped_column(Text, nullable=False)
    sport_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    parsed: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB, nullable=True)
    # timestamptz, UTC (ADR-0004 invariant 5).
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
