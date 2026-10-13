"""add training session tables

Revision ID: d3a7e5c1b942
Revises: b6f1c2d9e4a7
Create Date: 2026-10-13 10:00:00.000000

The four tables of ADR-0022 in one migration, so the schema is reviewed as a whole:
`training_sessions`, `training_session_sources`, `session_drafts` and `clarifications`. Every
reference to a user-owned row is keyed with `user_id` too, so no row can point at another user's
data, and no user's deletion can cascade into another's (ADR-0004 invariant 2, ADR-0021
invariant 1). Expand only (ADR-0024): new tables and one unique constraint on `chat_messages`
that its primary key already guarantees; no existing row is touched, nothing to backfill.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d3a7e5c1b942"
down_revision: str | Sequence[str] | None = "b6f1c2d9e4a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column[sa.DateTime]:
    return sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False)


def upgrade() -> None:
    # The target of the composite keys below; `id` alone is already unique.
    op.create_unique_constraint("chat_messages_id_user_id_key", "chat_messages", ["id", "user_id"])
    op.create_table(
        "training_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("sport_id", sa.String(), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("duration_min", sa.Integer(), nullable=True),
        sa.Column("rpe", sa.SmallInteger(), nullable=True),
        sa.Column("feeling", sa.String(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_version", sa.SmallInteger(), nullable=False),
        _created_at(),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "duration_min BETWEEN 1 AND 1440", name="training_sessions_duration_min_range"
        ),
        sa.CheckConstraint("rpe BETWEEN 1 AND 10", name="training_sessions_rpe_range"),
        sa.CheckConstraint("char_length(feeling) <= 200", name="training_sessions_feeling_length"),
        sa.CheckConstraint("char_length(notes) <= 2000", name="training_sessions_notes_length"),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="training_sessions_payload_object"
        ),
        sa.CheckConstraint("payload_version >= 1", name="training_sessions_payload_version"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "user_id", name="training_sessions_id_user_id_key"),
    )
    # History, reports and stats in M2; continuity and recent-session context (B16, B23).
    op.create_index(
        "ix_training_sessions_user_id_occurred_on",
        "training_sessions",
        ["user_id", sa.literal_column("occurred_on DESC")],
        unique=False,
    )
    op.create_index(
        "ix_training_sessions_user_id_sport_id_occurred_on",
        "training_sessions",
        ["user_id", "sport_id", sa.literal_column("occurred_on DESC")],
        unique=False,
    )

    op.create_table(
        "training_session_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("raw_text", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("logged_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("source IN ('chat', 'manual')", name="training_session_sources_source"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id", "user_id"],
            ["training_sessions.id", "training_sessions.user_id"],
            name="training_session_sources_session_id_user_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_training_session_sources_user_id"),
        "training_session_sources",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_training_session_sources_session_id_logged_at",
        "training_session_sources",
        ["session_id", "logged_at"],
        unique=False,
    )

    op.create_table(
        "session_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("sport_id", sa.String(), nullable=False),
        sa.Column("draft", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_version", sa.SmallInteger(), nullable=False),
        sa.Column("extends_session_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        _created_at(),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'discarded')", name="session_drafts_status"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        # A draft joins only a conversation of its own user (ADR-0021 invariant 1).
        sa.ForeignKeyConstraint(
            ["conversation_id", "user_id"],
            ["conversations.id", "conversations.user_id"],
            name="session_drafts_conversation_id_user_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id", "user_id"],
            ["chat_messages.id", "chat_messages.user_id"],
            name="session_drafts_message_id_user_id_fkey",
            ondelete="CASCADE",
        ),
        # SET NULL clears only the session column: `user_id` is NOT NULL and stays.
        sa.ForeignKeyConstraint(
            ["extends_session_id", "user_id"],
            ["training_sessions.id", "training_sessions.user_id"],
            name="session_drafts_extends_session_id_user_id_fkey",
            ondelete="SET NULL (extends_session_id)",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "user_id"],
            ["training_sessions.id", "training_sessions.user_id"],
            name="session_drafts_session_id_user_id_fkey",
            ondelete="SET NULL (session_id)",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "user_id", name="session_drafts_id_user_id_key"),
    )
    op.create_index(
        "ix_session_drafts_user_id_conversation_id_status",
        "session_drafts",
        ["user_id", "conversation_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_session_drafts_message_id"), "session_drafts", ["message_id"], unique=False
    )

    op.create_table(
        "clarifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=True),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("clarification", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("answer_option_ids", postgresql.ARRAY(sa.String()), nullable=True),
        _created_at(),
        sa.Column("answered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'answered', 'abandoned')", name="clarifications_status"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["conversation_id", "user_id"],
            ["conversations.id", "conversations.user_id"],
            name="clarifications_conversation_id_user_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id", "user_id"],
            ["chat_messages.id", "chat_messages.user_id"],
            name="clarifications_message_id_user_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["draft_id", "user_id"],
            ["session_drafts.id", "session_drafts.user_id"],
            name="clarifications_draft_id_user_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_clarifications_user_id_conversation_id_status",
        "clarifications",
        ["user_id", "conversation_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_clarifications_message_id"), "clarifications", ["message_id"], unique=False
    )
    op.create_index(
        op.f("ix_clarifications_draft_id"), "clarifications", ["draft_id"], unique=False
    )


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024).
    op.drop_table("clarifications")
    op.drop_table("session_drafts")
    op.drop_table("training_session_sources")
    op.drop_table("training_sessions")
    op.drop_constraint("chat_messages_id_user_id_key", "chat_messages", type_="unique")
