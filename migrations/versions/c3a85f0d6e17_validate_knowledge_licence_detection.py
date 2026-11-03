"""validate knowledge licence detection

Revision ID: c3a85f0d6e17
Revises: b7d41e9c2f63
Create Date: 2026-11-03 15:10:00.000000

Expand only (ADR-0024), second of two for #456: the CHECKs `b7d41e9c2f63` added NOT VALID are
validated in a transaction of their own, after the one that took ADD CONSTRAINT's lock. Reads
only: no row is written.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3a85f0d6e17"
down_revision: str | Sequence[str] | None = "b7d41e9c2f63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "knowledge_sources"
_CHECKS = (
    "knowledge_sources_status",
    "knowledge_sources_license_source",
    "knowledge_sources_licence_pair",
    "knowledge_sources_licence_set",
)


def upgrade() -> None:
    for name in _CHECKS:
        op.execute(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {name}")


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024). A validated
    # CHECK stays validated: NOT VALID can't be restored, and nothing depends on it.
    pass
