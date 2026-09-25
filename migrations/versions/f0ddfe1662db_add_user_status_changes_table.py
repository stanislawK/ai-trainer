"""add user status changes table

Revision ID: f0ddfe1662db
Revises: e4caa96bfbd1
Create Date: 2026-09-25 11:44:11.613546

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f0ddfe1662db"
down_revision: str | Sequence[str] | None = "e4caa96bfbd1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_status_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=False),
        sa.Column("old_status", sa.String(), nullable=False),
        sa.Column("new_status", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_user_status_changes_actor_user_id"),
        "user_status_changes",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_status_changes_target_user_id"),
        "user_status_changes",
        ["target_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_status_changes_target_user_id"), table_name="user_status_changes")
    op.drop_index(op.f("ix_user_status_changes_actor_user_id"), table_name="user_status_changes")
    op.drop_table("user_status_changes")
