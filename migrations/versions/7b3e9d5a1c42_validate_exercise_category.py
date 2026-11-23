"""validate exercise category

Revision ID: 7b3e9d5a1c42
Revises: 1ef0a5e35d86
Create Date: 2026-11-23 17:01:00.000000

Expand only (ADR-0024), second of two for #623: the CHECK `1ef0a5e35d86` added NOT VALID is
validated in a transaction of its own. Reads only: no row is written.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b3e9d5a1c42"
down_revision: str | Sequence[str] | None = "1ef0a5e35d86"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE exercises VALIDATE CONSTRAINT exercises_category")


def downgrade() -> None:
    # Dev and tests only; a validated CHECK stays validated and nothing depends on it.
    pass
