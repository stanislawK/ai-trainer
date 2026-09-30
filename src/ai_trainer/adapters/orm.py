import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    Date,
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UserOrm(Base):
    """Keyed by Google `sub`; no Google access or refresh token is ever stored (ADR-0005)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sub: Mapped[str] = mapped_column(unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(nullable=False)
    name: Mapped[str | None] = mapped_column(nullable=True)
    locale: Mapped[str] = mapped_column(nullable=False, server_default="en")
    # UserStatus value: pending / active / disabled (ADR-0005).
    status: Mapped[str] = mapped_column(nullable=False, server_default="pending")
    # timestamptz, UTC (ADR-0004 invariant 5).
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    # timestamptz, UTC; null until onboarding finishes (ADR-0006).
    onboarded_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class SessionOrm(Base):
    """A PostgreSQL-backed login session (ADR-0005): can be expired and revoked."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # timestamptz, UTC (ADR-0004 invariant 5).
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )


class UserStatusChangeOrm(Base):
    """One row per admin status change: actor, target, old/new status, timestamp
    (ADR-0005 invariant 9)."""

    __tablename__ = "user_status_changes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    old_status: Mapped[str] = mapped_column(nullable=False)
    new_status: Mapped[str] = mapped_column(nullable=False)
    # timestamptz, UTC (ADR-0004 invariant 5).
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )


class UserSportOrm(Base):
    """One row per sport the athlete trains (ADR-0006). `sport_id` is a `SportRegistry` ID
    stored as text and validated in the application layer, never a database enum, so a new
    sport needs no migration."""

    __tablename__ = "user_sports"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    sport_id: Mapped[str] = mapped_column(primary_key=True)
    # timestamptz, UTC (ADR-0004 invariant 5).
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )


class WeeklyAvailabilityOrm(Base):
    """One row per weekday the athlete can train (ADR-0006): weekday 0 is Monday, minutes is
    what they have that day (1-600). A day they cannot train has no row."""

    __tablename__ = "weekly_availability"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekly_availability_weekday_range"),
        CheckConstraint("minutes BETWEEN 1 AND 600", name="weekly_availability_minutes_range"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    weekday: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    minutes: Mapped[int] = mapped_column(SmallInteger)
    # timestamptz, UTC (ADR-0004 invariant 5).
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )


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


class LlmCallOrm(Base):
    """One row per gateway call, on success, timeout and error alike (ADR-0018)."""

    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    template_id: Mapped[str] = mapped_column(nullable=False)
    template_version: Mapped[int] = mapped_column(nullable=False)
    model: Mapped[str] = mapped_column(nullable=False)
    input_tokens: Mapped[int] = mapped_column(nullable=False)
    output_tokens: Mapped[int] = mapped_column(nullable=False)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int] = mapped_column(nullable=False)
    outcome: Mapped[str] = mapped_column(nullable=False)
    # timestamptz, UTC (ADR-0004 invariant 5).
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
