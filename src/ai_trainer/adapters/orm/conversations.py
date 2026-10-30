import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from ai_trainer.adapters.orm.base import Base


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
