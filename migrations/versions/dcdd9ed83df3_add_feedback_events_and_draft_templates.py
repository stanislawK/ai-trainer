"""add feedback events and draft templates

Revision ID: dcdd9ed83df3
Revises: a47987a22760
Create Date: 2026-10-31 16:13:27.293612

Expand only (ADR-0024): a new user-owned table and two nullable columns on `session_drafts`.
No existing row is read, rewritten or deleted, and drafts made before this keep NULL templates.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "dcdd9ed83df3"
down_revision: str | Sequence[str] | None = "a47987a22760"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feedback_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("template_id", sa.String(), nullable=True),
        sa.Column("template_version", sa.BigInteger(), nullable=True),
        sa.Column(
            "fields",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('draft_edited', 'draft_discarded', 'session_undone', 'choice_answered', "
            "'decision_overridden')",
            name="feedback_events_kind",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_feedback_events_user_id_created_at",
        "feedback_events",
        ["user_id", "created_at"],
        unique=False,
    )
    op.add_column("session_drafts", sa.Column("template_id", sa.String(), nullable=True))
    op.add_column("session_drafts", sa.Column("template_version", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("session_drafts", "template_version")
    op.drop_column("session_drafts", "template_id")
    op.drop_index("ix_feedback_events_user_id_created_at", table_name="feedback_events")
    op.drop_table("feedback_events")
