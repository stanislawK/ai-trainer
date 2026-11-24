"""feedback events kind picture flagged

Revision ID: d4a8e2b6f1c9
Revises: 9a6c3f1e5b27
Create Date: 2026-11-24 09:00:00.000000

Expand only (ADR-0024), first of two for #644 (ADR-0031 *Picture on the card*, *Flag*): an
athlete's "Looks wrong" on a picture drawn on the exercise card writes a content-free
`feedback_events` row of kind `picture_flagged`, so the kind CHECK is widened by that one value.
It is dropped and re-added NOT VALID, enforced on writes at once; `e5b9f3c7a2d0` validates it in a
transaction of its own.

Nothing is written, rewritten or deleted: every existing row's kind is in the old set, so it
passes the wider one. Release N-1's code writes only the old kinds, so an app rollback still
writes; once a `picture_flagged` row exists, N-1's `feedback-report` can't read it, so this
release is the report's rollback floor from the first flag on. Forward only.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a8e2b6f1c9"
down_revision: str | Sequence[str] | None = "9a6c3f1e5b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# squawk-ignore ban-drop-constraint: a widening, not a contract step (#644)
# The kind CHECK is dropped only to be re-added with one more value; no row or value is lost.

_TABLE = "feedback_events"
_CHECK = "feedback_events_kind"
_KINDS = (
    "'draft_edited', 'draft_discarded', 'session_undone', 'choice_answered', "
    "'decision_overridden', 'howto_flagged'"
)


def upgrade() -> None:
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CHECK} "
        f"CHECK (kind IN ({_KINDS}, 'picture_flagged')) NOT VALID"
    )


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024). The flags'
    # rows would break the narrower CHECK, so they go first.
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.execute(f"DELETE FROM {_TABLE} WHERE kind = 'picture_flagged'")
    op.create_check_constraint(_CHECK, _TABLE, f"kind IN ({_KINDS})")
