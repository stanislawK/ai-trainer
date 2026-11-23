"""validate goals kind

Revision ID: 9a6c3f1e5b27
Revises: 4d2b8e6f1a93
Create Date: 2026-11-23 21:01:00.000000

Expand only (ADR-0024), second of two for #625: the CHECK `4d2b8e6f1a93` added NOT VALID is
validated in a revision of its own. `migrations/env.py` runs one `upgrade head` in a single
transaction, so this shares the ADD COLUMN's lock; on `goals` (a few rows per athlete, every
existing kind NULL, which the CHECK allows) the scan takes milliseconds. Reads only: no row is
written.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9a6c3f1e5b27"
down_revision: str | Sequence[str] | None = "4d2b8e6f1a93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE goals VALIDATE CONSTRAINT goals_kind")


def downgrade() -> None:
    # Dev and tests only; a validated CHECK stays validated and nothing depends on it.
    pass
