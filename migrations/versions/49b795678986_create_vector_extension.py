"""create vector extension

Revision ID: 49b795678986
Revises:
Create Date: 2026-09-22 13:02:11.853475

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "49b795678986"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    # No CASCADE: a later migration that adds a `vector`-typed column must
    # drop it in its own downgrade() first (Alembic walks revisions in
    # reverse, so that already happens before this runs) — never widen this
    # to CASCADE just to make a broken downgrade order "work".
    op.execute("DROP EXTENSION IF EXISTS vector")
