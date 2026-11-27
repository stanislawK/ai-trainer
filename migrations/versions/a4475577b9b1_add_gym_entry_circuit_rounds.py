"""add gym entry circuit and rounds

Revision ID: a4475577b9b1
Revises: c71d4b9e2a58
Create Date: 2026-11-25 16:00:00.000000

Expand only (ADR-0024), first of two for #632 (ADR-0032 #618 amendment, PRD 0013 B43): an entry
may belong to a circuit, a group number within its session, done for a number of rounds, each
round a set. Two nullable columns with no default, so no table rewrite; every existing entry
reads NULL in both, as no circuit was ever stored. Their CHECKs are added NOT VALID, enforced on
writes at once, and `d076e3a63f7b` validates them.

Nothing is written, rewritten or deleted here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4475577b9b1"
down_revision: str | Sequence[str] | None = "c71d4b9e2a58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# squawk-ignore prefer-bigint-over-smallint: ADR-0032 types, bounded by their CHECKs (#632)

# (name, condition), as `GymExerciseEntryOrm` declares them.
CHECKS = (
    ("gym_exercise_entries_circuit_range", "circuit BETWEEN 1 AND 100"),
    ("gym_exercise_entries_rounds_range", "rounds BETWEEN 1 AND 100"),
    ("gym_exercise_entries_circuit_rounds", "(circuit IS NULL) = (rounds IS NULL)"),
)


def upgrade() -> None:
    op.add_column("gym_exercise_entries", sa.Column("circuit", sa.SmallInteger(), nullable=True))
    op.add_column("gym_exercise_entries", sa.Column("rounds", sa.SmallInteger(), nullable=True))
    for name, condition in CHECKS:
        op.execute(
            f"ALTER TABLE gym_exercise_entries ADD CONSTRAINT {name} CHECK ({condition}) NOT VALID"
        )


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024).
    for name, _ in reversed(CHECKS):
        op.drop_constraint(name, "gym_exercise_entries", type_="check")
    op.drop_column("gym_exercise_entries", "rounds")
    op.drop_column("gym_exercise_entries", "circuit")
