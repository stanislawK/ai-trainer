"""add exercise media removal

Revision ID: d5e2a8c41f07
Revises: 8f52610a9d41
Create Date: 2026-11-12 10:30:00.000000

Expand only (ADR-0024), first of two for #556 (ADR-0031's #556 amendment): an admin removes a
picture at once and can undo it.
- `removed_at`: when the link became `removed`, shown under the Removed filter.
- `removed_from`: the status Undo or Restore puts back, `suggested` or `approved`.

Both are nullable and set only when a link is removed; both are cleared on restore. Every
existing row keeps every value and gets NULL in both, which the CHECK accepts, so nothing is
backfilled or rewritten. A `removed` row with no `removed_from` restores to `suggested`. The
CHECK is added NOT VALID and enforced on writes at once; `e7a3c95b2d18` validates it in a
transaction of its own. Release N-1's code never reads either column, so an app rollback stays
safe (invariant 4).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e2a8c41f07"
down_revision: str | Sequence[str] | None = "8f52610a9d41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "exercise_media"
_CHECK = "exercise_media_removed_from"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("removed_from", sa.Text(), nullable=True))
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CHECK} "
        "CHECK (removed_from IN ('suggested', 'approved')) NOT VALID"
    )


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024).
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.drop_column(_TABLE, "removed_from")
    op.drop_column(_TABLE, "removed_at")
