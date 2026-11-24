"""validate gym set machine settings

Revision ID: c71d4b9e2a58
Revises: a3f8c2e6d914
Create Date: 2026-11-24 18:05:00.000000

Expand only (ADR-0024), second of two for #630: the machine settings' CHECKs `a3f8c2e6d914`
added NOT VALID are validated in a revision of their own, after the one that took ADD
CONSTRAINT's lock. Every existing row is NULL in them, so each passes. Reads only: no row is
written.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c71d4b9e2a58"
down_revision: str | Sequence[str] | None = "a3f8c2e6d914"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECKS = ("gym_sets_incline_pct", "gym_sets_machine_level", "gym_sets_watts")


def upgrade() -> None:
    for name in _CHECKS:
        op.execute(f"ALTER TABLE gym_sets VALIDATE CONSTRAINT {name}")


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024). A validated
    # CHECK stays validated: NOT VALID can't be restored, and nothing depends on it.
    pass
