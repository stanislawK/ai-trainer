"""add conversations

Revision ID: b6f1c2d9e4a7
Revises: 9a4c6e2b8d13
Create Date: 2026-10-12 10:00:00.000000

Puts every chat message in a conversation (ADR-0021) in expand -> backfill -> NOT NULL order,
so no existing row is lost or rewritten (ADR-0024): each user who has messages gets one
conversation holding all of them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6f1c2d9e4a7"
down_revision: str | Sequence[str] | None = "9a4c6e2b8d13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every character Python's `str.split()` splits on, spelled out rather than `[[:space:]]`, whose
# meaning depends on the database's collation.
_WHITESPACE = (
    r"[\t\n\u000b\f\r \u001c-\u001f\u0085\u00a0\u1680\u2000-\u200a"
    r"\u2028\u2029\u202f\u205f\u3000]+"
)


def title_sql(text: str) -> str:
    """`domain.chat.conversation_title` in SQL: whitespace collapsed to single spaces, then cut
    to 60 characters. `text` is a column or a bind parameter."""
    return f"rtrim(left(btrim(regexp_replace({text}, '{_WHITESPACE}', ' ', 'g')), 60))"


# One conversation per user with messages, created at their first message and last active at
# their latest, titled from their first athlete message. A user who already has a conversation
# is skipped, so the backfill is idempotent.
_BACKFILL_CONVERSATIONS = f"""
INSERT INTO conversations (id, user_id, title, created_at, last_message_at)
SELECT
    gen_random_uuid(),
    m.user_id,
    (
        SELECT {title_sql("f.text")}
        FROM chat_messages f
        WHERE f.user_id = m.user_id AND f.role = 'user'
        ORDER BY f.created_at, f.id
        LIMIT 1
    ),
    min(m.created_at),
    max(m.created_at)
FROM chat_messages m
WHERE m.conversation_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM conversations c WHERE c.user_id = m.user_id)
GROUP BY m.user_id
"""

# Every message still without a conversation joins its own user's one.
_ASSIGN_MESSAGES = """
UPDATE chat_messages m
SET conversation_id = c.id
FROM conversations c
WHERE c.user_id = m.user_id AND m.conversation_id IS NULL
"""

_NOT_NULL_CHECK = "chat_messages_conversation_id_not_null"


def upgrade() -> None:
    # 1. Expand: the new table and a nullable column.
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_message_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # The target of the messages' composite key below.
        sa.UniqueConstraint("id", "user_id", name="conversations_id_user_id_key"),
    )
    op.create_index(
        "ix_conversations_user_id_last_message_at",
        "conversations",
        ["user_id", sa.literal_column("last_message_at DESC")],
        unique=False,
    )
    op.add_column("chat_messages", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    # Keyed on (conversation_id, user_id), so a message can only join a conversation of its own
    # user (ADR-0021 invariant 1). NOT VALID now, validated once the backfill has filled it.
    op.execute(
        "ALTER TABLE chat_messages ADD CONSTRAINT chat_messages_conversation_id_user_id_fkey"
        " FOREIGN KEY (conversation_id, user_id) REFERENCES conversations (id, user_id)"
        " ON DELETE CASCADE NOT VALID"
    )

    # 2. Backfill.
    op.execute(_BACKFILL_CONVERSATIONS)
    op.execute(_ASSIGN_MESSAGES)
    op.execute(
        "ALTER TABLE chat_messages VALIDATE CONSTRAINT chat_messages_conversation_id_user_id_fkey"
    )

    op.create_index(
        "ix_chat_messages_conversation_id_created_at",
        "chat_messages",
        ["conversation_id", "created_at"],
        unique=False,
    )

    # 3. Require it. A validated CHECK lets SET NOT NULL skip its own full-table scan
    # (PostgreSQL 12+); the check is dropped once the column carries the constraint.
    op.execute(
        f"ALTER TABLE chat_messages ADD CONSTRAINT {_NOT_NULL_CHECK}"
        " CHECK (conversation_id IS NOT NULL) NOT VALID"
    )
    op.execute(f"ALTER TABLE chat_messages VALIDATE CONSTRAINT {_NOT_NULL_CHECK}")
    op.alter_column("chat_messages", "conversation_id", nullable=False)
    op.drop_constraint(_NOT_NULL_CHECK, "chat_messages", type_="check")


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024).
    op.drop_index("ix_chat_messages_conversation_id_created_at", table_name="chat_messages")
    op.drop_constraint(
        "chat_messages_conversation_id_user_id_fkey", "chat_messages", type_="foreignkey"
    )
    op.drop_column("chat_messages", "conversation_id")
    op.drop_index("ix_conversations_user_id_last_message_at", table_name="conversations")
    op.drop_table("conversations")
