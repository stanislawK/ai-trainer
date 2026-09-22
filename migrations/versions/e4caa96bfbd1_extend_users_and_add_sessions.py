"""extend users for google sign-in and add sessions

Revision ID: e4caa96bfbd1
Revises: fde2eaf4fd78
Create Date: 2026-09-22 22:05:18.792910

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4caa96bfbd1"
down_revision: str | Sequence[str] | None = "fde2eaf4fd78"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"], unique=False)
    op.add_column("users", sa.Column("sub", sa.String(), nullable=False))
    op.add_column("users", sa.Column("email", sa.String(), nullable=False))
    op.add_column("users", sa.Column("name", sa.String(), nullable=True))
    op.add_column("users", sa.Column("locale", sa.String(), server_default="en", nullable=False))
    op.add_column(
        "users", sa.Column("status", sa.String(), server_default="pending", nullable=False)
    )
    op.create_index(op.f("ix_users_sub"), "users", ["sub"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_users_sub"), table_name="users")
    op.drop_column("users", "status")
    op.drop_column("users", "locale")
    op.drop_column("users", "name")
    op.drop_column("users", "email")
    op.drop_column("users", "sub")
    op.drop_index(op.f("ix_sessions_user_id"), table_name="sessions")
    op.drop_table("sessions")
