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
    ForeignKeyConstraint,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
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
    # IANA name, e.g. Europe/Warsaw; UTC until the athlete confirms one (ADR-0014).
    timezone: Mapped[str] = mapped_column(nullable=False, server_default="UTC")


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


class ConversationOrm(Base):
    """One row per conversation (ADR-0021). `title` is the first athlete message, cut in code;
    `last_message_at` moves forward with every message and reply part. The index serves the
    list (B25) and the retention purge (B26)."""

    __tablename__ = "conversations"
    __table_args__ = (
        # The target of `chat_messages`' composite key (ADR-0021 invariant 1).
        UniqueConstraint("id", "user_id", name="conversations_id_user_id_key"),
        Index(
            "ix_conversations_user_id_last_message_at",
            "user_id",
            text("last_message_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(nullable=True)
    # timestamptz, UTC (ADR-0004 invariant 5).
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    last_message_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class ChatMessageOrm(Base):
    """One row per chat turn (ADR-0008), in one conversation of the same user (ADR-0021).
    `sports` (the sports a reply is about, PRD-0003 F15) and the template ID and version that
    produced a reply are null on an athlete's message."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="chat_messages_role"),
        Index("ix_chat_messages_conversation_id_created_at", "conversation_id", "created_at"),
        # The target of the training tables' composite keys (ADR-0022, ticket #154).
        UniqueConstraint("id", "user_id", name="chat_messages_id_user_id_key"),
        # A message joins only a conversation of its own user (ADR-0021 invariant 1).
        ForeignKeyConstraint(
            ["conversation_id", "user_id"],
            ["conversations.id", "conversations.user_id"],
            name="chat_messages_conversation_id_user_id_fkey",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    # ChatRole value: user / assistant.
    role: Mapped[str] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(nullable=False)
    # `SportRegistry` IDs stored as text, never a database enum (ADR-0006).
    sports: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    template_id: Mapped[str | None] = mapped_column(nullable=True)
    template_version: Mapped[int | None] = mapped_column(nullable=True)
    # timestamptz, UTC (ADR-0004 invariant 5).
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )


class TrainingSessionOrm(Base):
    """One row per session the athlete confirmed, the durable record (ADR-0022). It never
    references a conversation, message, draft or clarification, so the purge can't touch it
    (ADR-0022 invariant 1). `payload` belongs to the sport's plugin."""

    __tablename__ = "training_sessions"
    __table_args__ = (
        CheckConstraint(
            "duration_min BETWEEN 1 AND 1440", name="training_sessions_duration_min_range"
        ),
        CheckConstraint("rpe BETWEEN 1 AND 10", name="training_sessions_rpe_range"),
        CheckConstraint("char_length(feeling) <= 200", name="training_sessions_feeling_length"),
        CheckConstraint("char_length(notes) <= 2000", name="training_sessions_notes_length"),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="training_sessions_payload_object"
        ),
        CheckConstraint("payload_version >= 1", name="training_sessions_payload_version"),
        UniqueConstraint("id", "user_id", name="training_sessions_id_user_id_key"),
        Index(
            "ix_training_sessions_user_id_occurred_on",
            "user_id",
            text("occurred_on DESC"),
        ),
        Index(
            "ix_training_sessions_user_id_sport_id_occurred_on",
            "user_id",
            "sport_id",
            text("occurred_on DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # A `SportRegistry` ID, checked in the application layer, never a database enum (ADR-0006).
    sport_id: Mapped[str] = mapped_column(nullable=False)
    # The athlete's local calendar day, not a moment (ADR-0014 invariant 2 covers timestamps).
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    # The IANA zone `occurred_on` was resolved in.
    timezone: Mapped[str] = mapped_column(nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    duration_min: Mapped[int | None] = mapped_column(nullable=True)
    rpe: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    feeling: Mapped[str | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(nullable=True)
    payload: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False)
    payload_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # timestamptz, UTC, set from the `Clock` port (ADR-0014).
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class TrainingSessionSourceOrm(Base):
    """One row per log that created or extended a session, with its original text (B2,
    ADR-0022): copied, never a reference to a chat row."""

    __tablename__ = "training_session_sources"
    __table_args__ = (
        CheckConstraint("source IN ('chat', 'manual')", name="training_session_sources_source"),
        Index("ix_training_session_sources_session_id_logged_at", "session_id", "logged_at"),
        # A source joins only a session of its own user.
        ForeignKeyConstraint(
            ["session_id", "user_id"],
            ["training_sessions.id", "training_sessions.user_id"],
            name="training_session_sources_session_id_user_id_fkey",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    raw_text: Mapped[str] = mapped_column(nullable=False)
    source: Mapped[str] = mapped_column(nullable=False)
    # timestamptz, UTC: when the log was sent (B16 measures its merge window from here).
    logged_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class SessionDraftOrm(Base):
    """What the athlete hasn't confirmed yet (ADR-0022): transient, in one conversation of the
    same user, and gone with it when the conversation is purged (B26)."""

    __tablename__ = "session_drafts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'discarded')", name="session_drafts_status"
        ),
        Index(
            "ix_session_drafts_user_id_conversation_id_status",
            "user_id",
            "conversation_id",
            "status",
        ),
        # A draft joins only a conversation of its own user (ADR-0021 invariant 1).
        ForeignKeyConstraint(
            ["conversation_id", "user_id"],
            ["conversations.id", "conversations.user_id"],
            name="session_drafts_conversation_id_user_id_fkey",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["message_id", "user_id"],
            ["chat_messages.id", "chat_messages.user_id"],
            name="session_drafts_message_id_user_id_fkey",
            ondelete="CASCADE",
        ),
        # SET NULL clears only the session column: `user_id` is NOT NULL and stays.
        ForeignKeyConstraint(
            ["extends_session_id", "user_id"],
            ["training_sessions.id", "training_sessions.user_id"],
            name="session_drafts_extends_session_id_user_id_fkey",
            ondelete="SET NULL (extends_session_id)",
        ),
        ForeignKeyConstraint(
            ["session_id", "user_id"],
            ["training_sessions.id", "training_sessions.user_id"],
            name="session_drafts_session_id_user_id_fkey",
            ondelete="SET NULL (session_id)",
        ),
        UniqueConstraint("id", "user_id", name="session_drafts_id_user_id_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    # The athlete's log the draft came from.
    message_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    sport_id: Mapped[str] = mapped_column(nullable=False)
    # The validated `SessionDraft`: envelope fields, `date_source` and payload.
    draft: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False)
    payload_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # The session B16 offers to add this to.
    extends_session_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(nullable=False)
    # The session a confirm produced.
    session_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    # timestamptz, UTC; `resolved_at` is null while the draft is pending.
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class ClarificationOrm(Base):
    """An open question that suspends a draft or an intent until an option is picked
    (ADR-0015, ADR-0022). `purpose` is text checked in code, never an enum."""

    __tablename__ = "clarifications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'answered', 'abandoned')", name="clarifications_status"
        ),
        Index(
            "ix_clarifications_user_id_conversation_id_status",
            "user_id",
            "conversation_id",
            "status",
        ),
        ForeignKeyConstraint(
            ["conversation_id", "user_id"],
            ["conversations.id", "conversations.user_id"],
            name="clarifications_conversation_id_user_id_fkey",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["message_id", "user_id"],
            ["chat_messages.id", "chat_messages.user_id"],
            name="clarifications_message_id_user_id_fkey",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["draft_id", "user_id"],
            ["session_drafts.id", "session_drafts.user_id"],
            name="clarifications_draft_id_user_id_fkey",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    draft_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    purpose: Mapped[str] = mapped_column(nullable=False)
    clarification: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(nullable=False)
    answer_option_ids: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    # timestamptz, UTC; `answered_at` is null while the question is open.
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    answered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
