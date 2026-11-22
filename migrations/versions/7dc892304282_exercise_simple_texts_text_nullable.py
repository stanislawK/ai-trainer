"""exercise simple texts text nullable

Revision ID: 7dc892304282
Revises: 39448b3554e1
Create Date: 2026-11-22 22:00:00.000000

Expand only (ADR-0024), first of two for #591 (ADR-0031 *Jev check*, amended at the plan gate): a
summary that fails Jev's check once stores no text and counts the failure, so
`exercise_simple_texts.text` drops NOT NULL (a widening). A CHECK keeps the only row without a
text a `shown` one with a failed check (`check_failures > 0`), which is never shown and is written
again on the next card. It is added NOT VALID and enforced on writes at once; `4f2a9c61d8b3`
validates it in a transaction of its own.

Nothing is rewritten, backfilled or deleted: every existing row has a text, so it satisfies the
CHECK. Release N-1's code always writes a text, so an app rollback stays safe. Forward only:
restoring NOT NULL would be a contract step of its own owner-approved ticket, and none is planned.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7dc892304282"
down_revision: str | Sequence[str] | None = "39448b3554e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# squawk-ignore ban-drop-not-null: a widening, not a contract step (#591)
# `text` drops NOT NULL so a once-failed check can be counted without a text; no row or value is
# lost.

_TABLE = "exercise_simple_texts"
_CHECK = "exercise_simple_texts_text_or_failed_check"
_CONDITION = "text IS NOT NULL OR (status = 'shown' AND check_failures > 0)"


def upgrade() -> None:
    op.alter_column(_TABLE, "text", existing_type=sa.Text(), nullable=True)
    op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CHECK} CHECK ({_CONDITION}) NOT VALID")


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024). A row with
    # no text would block NOT NULL, so those once-failed rows go first: the next card rewrites
    # them.
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.execute(f"DELETE FROM {_TABLE} WHERE text IS NULL")
    op.alter_column(_TABLE, "text", existing_type=sa.Text(), nullable=False)
