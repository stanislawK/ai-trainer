"""add knowledge staging

Revision ID: 5c1e9b7d3a20
Revises: 2e3b50c913e0
Create Date: 2026-11-03 10:20:00.000000

Expand only (ADR-0024): the knowledge schema for staged sources (ADR-0036, #453), first of two.
- `knowledge_sources` gains the `proposed` and `rejected` statuses, the `jats` file type, the
  structured licence fields and the bibliographic identifiers.
- `license_note` loses NOT NULL (a widening): only an `other` licence needs one.
- A new `admin_tokens` table, owned by its admin and gone with the account.

The new CHECKs are added NOT VALID and enforced on writes at once; `9e4b2c7a1d58` validates them
and makes the identifiers unique, in a revision of its own so its concurrent index builds come
first (squawk reads a transaction followed by an autocommit block as no transaction at all).

Existing rows read `license_id = 'other'` and `license_source = 'admin'` from constant column
defaults: no table rewrite and no UPDATE. Their notes are untouched, and the CHECK that `other`
has a note holds for them, since #422 required one. Release N's code inserts without the new
columns and still passes every CHECK, so an app rollback stays safe (invariant 4) while every
row is still one it can read. A later ticket's first `proposed` source or note-less CC source
is not: #422's code can't read it, so that ticket's release, or this one, is the rollback floor.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5c1e9b7d3a20"
down_revision: str | Sequence[str] | None = "2e3b50c913e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# squawk-ignore ban-drop-constraint, ban-drop-not-null: widenings, not contract steps (#453)
# The status and file-type CHECKs are dropped only to be re-added with more values, and
# `license_note` drops NOT NULL so a CC source needs no note; no row or value is lost.

_TABLE = "knowledge_sources"
_IDENTIFIERS = ("pmcid", "doi", "pmid", "isbn")

# Replaced by a wider set: (old, new).
_WIDENED = {
    "knowledge_sources_status": (
        "status IN ('uploaded', 'ingesting', 'ingested', 'failed')",
        "status IN ('proposed', 'rejected', 'uploaded', 'ingesting', 'ingested', 'failed')",
    ),
    "knowledge_sources_file_type": (
        "file_type IN ('pdf', 'epub', 'markdown', 'text', 'vtt', 'srt')",
        "file_type IN ('pdf', 'epub', 'markdown', 'text', 'vtt', 'srt', 'jats')",
    ),
}
_CHECKS = {
    "knowledge_sources_license_id": (
        "license_id IN ('cc0', 'cc-by', 'cc-by-sa', 'cc-by-nc', 'cc-by-nc-sa', 'cc-by-nd', "
        "'cc-by-nc-nd', 'other')"
    ),
    "knowledge_sources_license_source": "license_source IN ('pmc', 'jats', 'admin')",
    # With #422's `btrim(license_note) <> ''`, an `other` licence needs a non-blank note.
    "knowledge_sources_other_license_note": "license_id <> 'other' OR license_note IS NOT NULL",
    "knowledge_sources_pmcid_format": "pmcid ~ '^PMC[0-9]+$'",
    "knowledge_sources_pmid_format": "pmid ~ '^[0-9]+$'",
    # A bare DOI, never its URL, and lowercased: DOIs are case-insensitive, so the unique key
    # then catches the same DOI in any spelling.
    "knowledge_sources_doi_format": "doi ~ '^10\\.[0-9.]+/.' AND doi = lower(doi)",
}


def upgrade() -> None:
    op.add_column(
        _TABLE, sa.Column("license_id", sa.String(), server_default="other", nullable=False)
    )
    op.add_column(
        _TABLE, sa.Column("license_source", sa.String(), server_default="admin", nullable=False)
    )
    for column in ("license_url", "copyright_statement", "attribution", *_IDENTIFIERS):
        op.add_column(_TABLE, sa.Column(column, sa.Text(), nullable=True))
    op.alter_column(_TABLE, "license_note", existing_type=sa.Text(), nullable=True)
    for name in _WIDENED:
        op.drop_constraint(name, _TABLE, type_="check")
    checks = {name: wider for name, (_, wider) in _WIDENED.items()} | _CHECKS
    for name, condition in checks.items():
        op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {name} CHECK ({condition}) NOT VALID")

    op.create_table(
        "admin_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("display_prefix", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="admin_tokens_token_hash"),
        sa.CheckConstraint("btrim(display_prefix) <> ''", name="admin_tokens_display_prefix"),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + interval '30 days'",
            name="admin_tokens_expires_at",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="admin_tokens_token_hash_key"),
    )
    op.create_index("ix_admin_tokens_user_id", "admin_tokens", ["user_id"])


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024).
    op.drop_index("ix_admin_tokens_user_id", table_name="admin_tokens")
    op.drop_table("admin_tokens")
    for name in _CHECKS:
        op.drop_constraint(name, _TABLE, type_="check")
    for name, (narrower, _) in _WIDENED.items():
        op.drop_constraint(name, _TABLE, type_="check")
        op.create_check_constraint(name, _TABLE, narrower)
    op.alter_column(_TABLE, "license_note", existing_type=sa.Text(), nullable=False)
    for column in (
        *_IDENTIFIERS,
        "attribution",
        "copyright_statement",
        "license_url",
        "license_source",
        "license_id",
    ):
        op.drop_column(_TABLE, column)
