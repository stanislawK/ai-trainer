"""add gym set machine settings

Revision ID: a3f8c2e6d914
Revises: 5e9a2c7b4f18
Create Date: 2026-11-24 18:00:00.000000

Expand only (ADR-0024), first of two for #630 (ADR-0032 #618 amendment, PRD 0013 B44): a set
may record its machine's incline in %, level and watts, each only as the athlete states it.
Three nullable columns with no default, so no table rewrite; every existing set reads NULL in
them, as no setting was ever stated before. Their CHECKs are added NOT VALID, enforced on writes
at once, and `c71d4b9e2a58` validates them.

Nothing is written, rewritten or deleted here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f8c2e6d914"
down_revision: str | Sequence[str] | None = "5e9a2c7b4f18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# squawk-ignore prefer-bigint-over-smallint: ADR-0032 types, bounded by their CHECKs (#630)

# (name, condition), as `GymSetOrm` declares them.
CHECKS = (
    ("gym_sets_incline_pct", "incline_pct BETWEEN -10 AND 40"),
    ("gym_sets_machine_level", "machine_level BETWEEN 1 AND 50"),
    ("gym_sets_watts", "watts BETWEEN 1 AND 2000"),
)


def upgrade() -> None:
    op.add_column("gym_sets", sa.Column("incline_pct", sa.Numeric(4, 1), nullable=True))
    op.add_column("gym_sets", sa.Column("machine_level", sa.SmallInteger(), nullable=True))
    op.add_column("gym_sets", sa.Column("watts", sa.SmallInteger(), nullable=True))
    for name, condition in CHECKS:
        op.execute(f"ALTER TABLE gym_sets ADD CONSTRAINT {name} CHECK ({condition}) NOT VALID")


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024).
    for name, _ in reversed(CHECKS):
        op.drop_constraint(name, "gym_sets", type_="check")
    op.drop_column("gym_sets", "watts")
    op.drop_column("gym_sets", "machine_level")
    op.drop_column("gym_sets", "incline_pct")
