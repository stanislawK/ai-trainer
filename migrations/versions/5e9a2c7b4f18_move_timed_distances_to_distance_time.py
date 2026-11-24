"""move timed distances to distance time

Revision ID: 5e9a2c7b4f18
Revises: 8b2f6d4e1a73
Create Date: 2026-11-24 16:10:00.000000

Expand only (ADR-0024), third of three for #626 (ADR-0032 #618 amendment). The narrower measure
CHECKs are dropped now that the validated wider ones stand in for them, so no value they
admitted is refused. Then a gym entry measured by `time` or `distance` whose first set holding
any value (lowest `set_no`) has both a time and a distance moves to `distance_time`: the rule
`measure_of` reads a set by and the edit applies to an exercise's first given set, so the rows
read as a new log or edit of them would. Its sets are untouched, so every distance and time
keeps its stored value. The UPDATE has a WHERE, matches sets on the entry's `user_id`, runs
again as a no-op, and touches only gym entries, which are few. The upgrade-with-data check
declares the one seeded row it moves (`RECLASSIFIES`, ADR-0024 #626 amendment).

Once a `distance_time` row exists, release N-1's closed measure set can't read it, so an app
rollback past this release needs a fix forward (owner-accepted at #626's plan gate).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5e9a2c7b4f18"
down_revision: str | Sequence[str] | None = "8b2f6d4e1a73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# squawk-ignore ban-drop-constraint: a widening, not a contract step (#626)
# The old CHECKs give way to the validated wider ones; no row or value is lost.

_NARROWER = "'load_reps', 'reps', 'time', 'distance', 'band_reps'"

# The rows `measure_of` would read as `distance_time`: the first set with any value holds both.
# A predicate over `gym_exercise_entries` aliased `t`.
MOVED = """
    t.measure IN ('time', 'distance')
    AND EXISTS (
        SELECT 1
        FROM (
            SELECT s.duration_s, s.distance_m
            FROM gym_sets AS s
            WHERE s.entry_id = t.id
              AND s.user_id = t.user_id
              AND (
                  s.reps IS NOT NULL OR s.load_kg IS NOT NULL OR s.duration_s IS NOT NULL
                  OR s.distance_m IS NOT NULL OR s.band_label IS NOT NULL
              )
            ORDER BY s.set_no
            LIMIT 1
        ) AS first_given
        WHERE first_given.duration_s IS NOT NULL AND first_given.distance_m IS NOT NULL
    )
"""

# The upgrade-with-data check expects `distance_time` on exactly these rows (ADR-0024, #626).
RECLASSIFIES = {("gym_exercise_entries", "measure"): ("distance_time", MOVED)}


def upgrade() -> None:
    op.drop_constraint("gym_exercise_entries_measure", "gym_exercise_entries", type_="check")
    op.drop_constraint("exercises_default_measure", "exercises", type_="check")
    op.execute(f"UPDATE gym_exercise_entries AS t SET measure = 'distance_time' WHERE {MOVED}")


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024). The moved
    # rows read `time` again, so the narrower CHECKs fit.
    op.execute("UPDATE gym_exercise_entries SET measure = 'time' WHERE measure = 'distance_time'")
    op.execute(
        "UPDATE exercises SET default_measure = 'time' WHERE default_measure = 'distance_time'"
    )
    op.create_check_constraint(
        "gym_exercise_entries_measure", "gym_exercise_entries", f"measure IN ({_NARROWER})"
    )
    op.create_check_constraint(
        "exercises_default_measure", "exercises", f"default_measure IN ({_NARROWER})"
    )
