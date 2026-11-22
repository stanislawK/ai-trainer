"""add exercise simple texts

Revision ID: 39448b3554e1
Revises: e7a3c95b2d18
Create Date: 2026-11-22 20:37:37.830387

Expand only (ADR-0024), for #589 (ADR-0031 *Beginner how-to*): `exercise_simple_texts`, one
beginner how-to per catalogue exercise and language, written by a text model from the exercise's
own catalogue text. A shared reference table with no user column, like the catalogue; rows go with
their exercise (CASCADE). The key `(exercise_id, lang)` is the "first write wins" target and
indexes `exercise_id`.
- `source_sha256`: the hash of the instructions the text was written from; a mismatch is stale.
- `status`: `shown`, `flagged` or `rejected`. `check_version`: the Jev check's pinned version,
  NULL until that check lands. `check_failures`: Jev's failed checks of the current source.
- `flagged_at`, `edited_at`: when an athlete flagged it, when an admin edited it (an edited text is
  never rewritten).

A new table only: no existing table or row changes, nothing is backfilled (rows are written on
first display). Release N-1's code never reads it, so an app rollback stays safe.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "39448b3554e1"
down_revision: str | Sequence[str] | None = "e7a3c95b2d18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "exercise_simple_texts",
        sa.Column("exercise_id", sa.Uuid(), nullable=False),
        sa.Column("lang", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("template_id", sa.Text(), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="shown", nullable=False),
        sa.Column("check_version", sa.Integer(), nullable=True),
        sa.Column("check_failures", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("flagged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('shown', 'flagged', 'rejected')", name="exercise_simple_texts_status"
        ),
        sa.CheckConstraint(
            "char_length(source_sha256) = 64", name="exercise_simple_texts_source_sha256"
        ),
        sa.CheckConstraint("check_failures >= 0", name="exercise_simple_texts_check_failures"),
        sa.ForeignKeyConstraint(["exercise_id"], ["exercises.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("exercise_id", "lang"),
    )


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024).
    op.drop_table("exercise_simple_texts")
