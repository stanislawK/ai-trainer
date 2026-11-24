"""validate distance time measure

Revision ID: 8b2f6d4e1a73
Revises: 3c7e1a9f5d24
Create Date: 2026-11-24 16:05:00.000000

Expand only (ADR-0024), second of three for #626: the wider measure CHECKs `3c7e1a9f5d24` added
NOT VALID are validated in a revision of their own, after the one that took ADD CONSTRAINT's
lock. Reads only: no row is written.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8b2f6d4e1a73"
down_revision: str | Sequence[str] | None = "3c7e1a9f5d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE gym_exercise_entries VALIDATE CONSTRAINT gym_exercise_entries_measure_v2"
    )
    op.execute("ALTER TABLE exercises VALIDATE CONSTRAINT exercises_default_measure_v2")


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024). A validated
    # CHECK stays validated: NOT VALID can't be restored, and nothing depends on it.
    pass
