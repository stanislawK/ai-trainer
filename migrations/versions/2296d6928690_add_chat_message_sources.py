"""add chat message sources

Revision ID: 2296d6928690
Revises: fe77f6d38ca0
Create Date: 2026-11-06 17:48:50.432566

Expand only (ADR-0024), for #428:
- `chat_messages` gains a nullable `sources` JSONB column: the knowledge sources a cited reply
  used, each a snapshot of title, author, evidence tier, source type, attribution and locator,
  with no passage text (PRD B8, ADR-0036 #428 amendment). No default, no index, no backfill:
  every existing row reads NULL, which is "cites nothing", since no reply before this one cited
  the knowledge base. Adding a nullable column with no default takes a brief lock only.

Release N's code never reads it, so an app rollback stays safe (invariant 4).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "2296d6928690"
down_revision: str | Sequence[str] | None = "fe77f6d38ca0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("sources", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "sources")
