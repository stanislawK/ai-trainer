"""validate exercise media removal

Revision ID: e7a3c95b2d18
Revises: d5e2a8c41f07
Create Date: 2026-11-12 10:31:00.000000

Expand only (ADR-0024), second of two for #556: the CHECK `d5e2a8c41f07` added NOT VALID is
validated in a transaction of its own, after the one that took ADD CONSTRAINT's lock. Reads
only: no row is written.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7a3c95b2d18"
down_revision: str | Sequence[str] | None = "d5e2a8c41f07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE exercise_media VALIDATE CONSTRAINT exercise_media_removed_from")


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024). A validated
    # CHECK stays validated: NOT VALID can't be restored, and nothing depends on it.
    pass
