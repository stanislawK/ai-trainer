import uuid
from datetime import date, datetime

from pydantic import JsonValue
from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ai_trainer.adapters.orm.base import Base


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
        # A sport's rows name the log that brought them, keyed with `user_id` (ADR-0032).
        UniqueConstraint("id", "user_id", name="training_session_sources_id_user_id_key"),
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
    # The extraction template the draft came from (ADR-0034); NULL for a draft made before.
    template_id: Mapped[str | None] = mapped_column(nullable=True)
    template_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
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
