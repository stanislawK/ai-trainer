"""feedback events kind howto flagged

Revision ID: b3d7e1f04a62
Revises: 4f2a9c61d8b3
Create Date: 2026-11-22 23:40:00.000000

Expand only (ADR-0024), first of two for #593 (ADR-0031 *The flag*, *Consequences*): an athlete's
"Looks wrong" on a beginner how-to writes a content-free `feedback_events` row of kind
`howto_flagged`, so the kind CHECK is widened by that one value. It is dropped and re-added
NOT VALID, enforced on writes at once; `c8f2a5d9e317` validates it in a transaction of its own.

Nothing is written, rewritten or deleted: every existing row's kind is in the old set, so it
passes the wider one. Release N-1's code writes only the old kinds, so an app rollback still
writes; once a `howto_flagged` row exists, N-1's `feedback-report` can't read it, so this release
is the report's rollback floor from the first flag on. Forward only.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3d7e1f04a62"
down_revision: str | Sequence[str] | None = "4f2a9c61d8b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# squawk-ignore ban-drop-constraint: a widening, not a contract step (#593)
# The kind CHECK is dropped only to be re-added with one more value; no row or value is lost.

_TABLE = "feedback_events"
_CHECK = "feedback_events_kind"
_KINDS = (
    "'draft_edited', 'draft_discarded', 'session_undone', 'choice_answered', 'decision_overridden'"
)


def upgrade() -> None:
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CHECK} "
        f"CHECK (kind IN ({_KINDS}, 'howto_flagged')) NOT VALID"
    )


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024). The flags'
    # rows would break the narrower CHECK, so they go first.
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.execute(f"DELETE FROM {_TABLE} WHERE kind = 'howto_flagged'")
    op.create_check_constraint(_CHECK, _TABLE, f"kind IN ({_KINDS})")
