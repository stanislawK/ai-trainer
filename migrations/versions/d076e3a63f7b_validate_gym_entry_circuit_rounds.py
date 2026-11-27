"""validate gym entry circuit and rounds

Revision ID: d076e3a63f7b
Revises: a4475577b9b1
Create Date: 2026-11-25 16:05:00.000000

Expand only (ADR-0024), second of two for #632: the circuit CHECKs `a4475577b9b1` added NOT
VALID are validated in a revision of their own, after the one that took ADD CONSTRAINT's lock.
Every existing row is NULL in both columns, so each passes. Reads only: no row is written.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d076e3a63f7b"
down_revision: str | Sequence[str] | None = "a4475577b9b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECKS = (
    "gym_exercise_entries_circuit_range",
    "gym_exercise_entries_rounds_range",
    "gym_exercise_entries_circuit_rounds",
)


def upgrade() -> None:
    for name in _CHECKS:
        op.execute(f"ALTER TABLE gym_exercise_entries VALIDATE CONSTRAINT {name}")


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024). A validated
    # CHECK stays validated: NOT VALID can't be restored, and nothing depends on it.
    pass
