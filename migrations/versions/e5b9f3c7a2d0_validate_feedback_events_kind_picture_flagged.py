"""validate feedback events kind picture flagged

Revision ID: e5b9f3c7a2d0
Revises: d4a8e2b6f1c9
Create Date: 2026-11-24 09:05:00.000000

Expand only (ADR-0024), second of two for #644: the kind CHECK `d4a8e2b6f1c9` re-added NOT VALID
is validated in a transaction of its own, after the one that took ADD CONSTRAINT's lock. Reads
only: no row is written.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5b9f3c7a2d0"
down_revision: str | Sequence[str] | None = "d4a8e2b6f1c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE feedback_events VALIDATE CONSTRAINT feedback_events_kind")


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024). A validated
    # CHECK stays validated: NOT VALID can't be restored, and nothing depends on it.
    pass
