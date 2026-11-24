"""add distance time measure

Revision ID: 3c7e1a9f5d24
Revises: e5b9f3c7a2d0
Create Date: 2026-11-24 16:00:00.000000

Expand only (ADR-0024), first of three for #626 (ADR-0032 #618 amendment): a set holding both a
distance and a time is measured by `distance_time`. The wider measure CHECKs are added beside
the old ones NOT VALID, enforced on writes at once: `gym_exercise_entries_measure_v2`, and
`exercises_default_measure_v2`, since an athlete's own exercise takes its entry's measure as
its default (ADR-0031 #618 amendment: `default_measure` may be `distance_time`). While both
CHECKs stand, a row still needs the old set. `8b2f6d4e1a73` validates the new ones and
`5e9a2c7b4f18` drops the old ones and moves the timed distances.

Nothing is written, rewritten or deleted here.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c7e1a9f5d24"
down_revision: str | Sequence[str] | None = "e5b9f3c7a2d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WIDER = "'load_reps', 'reps', 'time', 'distance', 'distance_time', 'band_reps'"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE gym_exercise_entries ADD CONSTRAINT gym_exercise_entries_measure_v2 "
        f"CHECK (measure IN ({_WIDER})) NOT VALID"
    )
    op.execute(
        "ALTER TABLE exercises ADD CONSTRAINT exercises_default_measure_v2 "
        f"CHECK (default_measure IN ({_WIDER})) NOT VALID"
    )


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024).
    op.drop_constraint("exercises_default_measure_v2", "exercises", type_="check")
    op.drop_constraint("gym_exercise_entries_measure_v2", "gym_exercise_entries", type_="check")
