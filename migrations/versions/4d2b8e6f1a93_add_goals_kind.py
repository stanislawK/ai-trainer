"""add goals kind

Revision ID: 4d2b8e6f1a93
Revises: 7b3e9d5a1c42
Create Date: 2026-11-23 21:00:00.000000

Expand only (ADR-0024), first of two for #625 (ADR-0028's #618 amendment, PRD 0013 B45):
`goals.kind` is a nullable text column, so every existing goal reads NULL (no kind, which B45
allows) and none is rewritten or backfilled. The CHECK is added NOT VALID and enforced on writes
at once; `9a6c3f1e5b27` validates it in a revision of its own. Release N-1's code never names
the column, so it runs on this schema.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4d2b8e6f1a93"
down_revision: str | Sequence[str] | None = "7b3e9d5a1c42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("goals", sa.Column("kind", sa.Text(), nullable=True))
    op.execute(
        "ALTER TABLE goals ADD CONSTRAINT goals_kind CHECK "
        "(kind IN ('strength', 'muscle', 'endurance', 'weight_loss', 'mobility', "
        "'general_fitness')) NOT VALID"
    )


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024).
    op.drop_constraint("goals_kind", "goals", type_="check")
    op.drop_column("goals", "kind")
