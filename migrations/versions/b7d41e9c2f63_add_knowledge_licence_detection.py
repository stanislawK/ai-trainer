"""add knowledge licence detection

Revision ID: b7d41e9c2f63
Revises: 9e4b2c7a1d58
Create Date: 2026-11-03 15:00:00.000000

Expand only (ADR-0024), first of two for #456 (ADR-0036's #454 amendment): a form upload may
leave its licence to be detected from the file.
- The status set gains `detecting`, and `license_source` gains `file`.
- `license_id` and `license_source` lose NOT NULL (a widening): a `detecting` or `proposed`
  source may wait without one, written as explicit NULLs. #453's defaults stay, so an insert
  that names neither still reads `other`/`admin`. Two CHECKs keep the rest licensed: both are
  set together, and both are set unless the status is `detecting` or `proposed`.
- Three nullable columns hold what detection found: the matched line, where it was, and why no
  licence was set.

The CHECKs are added NOT VALID and enforced on writes at once; `c3a85f0d6e17` validates them in
a transaction of its own. Every existing row has both licence fields set (#453's defaults), so
each one passes, and none is rewritten or updated. Release N-1's code reads both fields as set,
so this release's first `detecting` row is its rollback floor, as #453's first `proposed` one is.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d41e9c2f63"
down_revision: str | Sequence[str] | None = "9e4b2c7a1d58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# squawk-ignore ban-drop-constraint, ban-drop-not-null: widenings, not contract steps (#456)
# The status and licence-source CHECKs are dropped only to be re-added with one more value, and
# the licence fields drop NOT NULL so a source may wait for its licence; no row or value is lost.

_TABLE = "knowledge_sources"
_DETECTION = ("license_matched_line", "license_locator", "license_detection_reason")

# Replaced by a wider set: (old, new).
_WIDENED = {
    "knowledge_sources_status": (
        "status IN ('proposed', 'rejected', 'uploaded', 'ingesting', 'ingested', 'failed')",
        "status IN ('detecting', 'proposed', 'rejected', 'uploaded', 'ingesting', 'ingested', "
        "'failed')",
    ),
    "knowledge_sources_license_source": (
        "license_source IN ('pmc', 'jats', 'admin')",
        "license_source IN ('pmc', 'jats', 'admin', 'file')",
    ),
}
_CHECKS = {
    "knowledge_sources_licence_pair": "(license_id IS NULL) = (license_source IS NULL)",
    "knowledge_sources_licence_set": (
        "status IN ('detecting', 'proposed') OR license_id IS NOT NULL"
    ),
}


def upgrade() -> None:
    for column in ("license_id", "license_source"):
        op.alter_column(_TABLE, column, existing_type=sa.String(), nullable=True)
    for column in _DETECTION:
        op.add_column(_TABLE, sa.Column(column, sa.Text(), nullable=True))
    for name in _WIDENED:
        op.drop_constraint(name, _TABLE, type_="check")
    checks = {name: wider for name, (_, wider) in _WIDENED.items()} | _CHECKS
    for name, condition in checks.items():
        op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {name} CHECK ({condition}) NOT VALID")


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024).
    for name in _CHECKS:
        op.drop_constraint(name, _TABLE, type_="check")
    for name, (narrower, _) in _WIDENED.items():
        op.drop_constraint(name, _TABLE, type_="check")
        op.create_check_constraint(name, _TABLE, narrower)
    for column in reversed(_DETECTION):
        op.drop_column(_TABLE, column)
    for column in ("license_source", "license_id"):
        op.alter_column(_TABLE, column, existing_type=sa.String(), nullable=False)
