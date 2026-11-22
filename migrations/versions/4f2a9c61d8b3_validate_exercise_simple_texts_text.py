"""validate exercise simple texts text

Revision ID: 4f2a9c61d8b3
Revises: 7dc892304282
Create Date: 2026-11-22 22:10:00.000000

Expand only (ADR-0024), second of two for #591: the CHECK `7dc892304282` added NOT VALID is
validated in a transaction of its own, after the one that took ADD CONSTRAINT's lock. Reads
only: no row is written.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4f2a9c61d8b3"
down_revision: str | Sequence[str] | None = "7dc892304282"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE exercise_simple_texts "
        "VALIDATE CONSTRAINT exercise_simple_texts_text_or_failed_check"
    )


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024). A validated
    # CHECK stays validated: NOT VALID can't be restored, and nothing depends on it.
    pass
