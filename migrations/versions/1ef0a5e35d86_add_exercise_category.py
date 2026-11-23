"""add exercise category

Revision ID: 1ef0a5e35d86
Revises: c8f2a5d9e317
Create Date: 2026-11-23 17:00:00.000000

Expand only (ADR-0024), first of two for #623 (ADR-0031): `exercises.category` is
`text NOT NULL DEFAULT 'strength'`, so every existing row, catalogue or custom, reads `strength`
at once and none is rewritten (a constant default needs no table rewrite). The import writes the
catalogue rows' real category on its next run. The CHECK is added NOT VALID and enforced on
writes at once; `7b3e9d5a1c42` validates it in a transaction of its own.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1ef0a5e35d86"
down_revision: str | Sequence[str] | None = "c8f2a5d9e317"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "exercises",
        sa.Column("category", sa.Text(), server_default="strength", nullable=False),
    )
    op.execute(
        "ALTER TABLE exercises ADD CONSTRAINT exercises_category CHECK "
        "(category IN ('strength', 'cardio', 'stretching', 'mobility', 'class')) NOT VALID"
    )


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024).
    op.drop_constraint("exercises_category", "exercises", type_="check")
    op.drop_column("exercises", "category")
