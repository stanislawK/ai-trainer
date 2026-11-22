"""validate feedback events kind

Revision ID: c8f2a5d9e317
Revises: b3d7e1f04a62
Create Date: 2026-11-22 23:45:00.000000

Expand only (ADR-0024), second of two for #593: the kind CHECK `b3d7e1f04a62` re-added NOT VALID
is validated in a transaction of its own, after the one that took ADD CONSTRAINT's lock. Reads
only: no row is written.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8f2a5d9e317"
down_revision: str | Sequence[str] | None = "b3d7e1f04a62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE feedback_events VALIDATE CONSTRAINT feedback_events_kind")


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024). A validated
    # CHECK stays validated: NOT VALID can't be restored, and nothing depends on it.
    pass
