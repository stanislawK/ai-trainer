"""validate knowledge staging

Revision ID: 9e4b2c7a1d58
Revises: 5c1e9b7d3a20
Create Date: 2026-11-03 10:40:00.000000

Expand only (ADR-0024), second of two for #453 (ADR-0036): the bibliographic identifiers become
unique when set, and the CHECKs `5c1e9b7d3a20` added NOT VALID are validated. Reads only:
no row is written, and existing rows have no identifiers, so none can collide.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9e4b2c7a1d58"
down_revision: str | Sequence[str] | None = "5c1e9b7d3a20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "knowledge_sources"
_IDENTIFIERS = ("pmcid", "doi", "pmid", "isbn")
_CHECKS = (
    "knowledge_sources_status",
    "knowledge_sources_file_type",
    "knowledge_sources_license_id",
    "knowledge_sources_license_source",
    "knowledge_sources_other_license_note",
    "knowledge_sources_pmcid_format",
    "knowledge_sources_pmid_format",
    "knowledge_sources_doi_format",
)


def upgrade() -> None:
    # Unique when set (NULLs never collide): built concurrently, then attached, so the existing
    # table is never locked for the build. The block also commits the NOT VALID CHECKs, so the
    # VALIDATE scans run in a later transaction than the lock ADD CONSTRAINT took.
    with op.get_context().autocommit_block():
        for column in _IDENTIFIERS:
            op.create_index(
                f"{_TABLE}_{column}_key",
                _TABLE,
                [column],
                unique=True,
                postgresql_concurrently=True,
                if_not_exists=True,
            )
    for name in _CHECKS:
        op.execute(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {name}")
    for column in _IDENTIFIERS:
        key = f"{_TABLE}_{column}_key"
        op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {key} UNIQUE USING INDEX {key}")


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024). A validated
    # CHECK stays validated: NOT VALID can't be restored, and nothing depends on it.
    for column in _IDENTIFIERS:
        op.drop_constraint(f"{_TABLE}_{column}_key", _TABLE, type_="unique")
