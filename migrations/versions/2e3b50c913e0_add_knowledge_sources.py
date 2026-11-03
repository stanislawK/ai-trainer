"""add knowledge sources

Revision ID: 2e3b50c913e0
Revises: dcdd9ed83df3
Create Date: 2026-11-02 19:39:00.746332

Expand only (ADR-0024): one new table, the knowledge base's manifest (ADR-0011, ADR-0033, #422).
No existing table or row is read, rewritten or deleted, and nothing is backfilled.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "2e3b50c913e0"
down_revision: str | Sequence[str] | None = "dcdd9ed83df3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("evidence_tier", sa.String(), nullable=False),
        sa.Column("language", sa.String(), nullable=False),
        sa.Column("sports", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "topics",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("license_note", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("file_type", sa.String(), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), server_default="uploaded", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("uploaded_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source_type IN ('research_paper', 'book', 'talk_or_podcast', 'article', 'catalogue')",
            name="knowledge_sources_source_type",
        ),
        sa.CheckConstraint(
            "evidence_tier IN ('peer_reviewed', 'coaching_book', 'article_or_transcript', "
            "'catalogue')",
            name="knowledge_sources_evidence_tier",
        ),
        sa.CheckConstraint("language IN ('en', 'pl')", name="knowledge_sources_language"),
        sa.CheckConstraint(
            "status IN ('uploaded', 'ingesting', 'ingested', 'failed')",
            name="knowledge_sources_status",
        ),
        sa.CheckConstraint(
            "file_type IN ('pdf', 'epub', 'markdown', 'text', 'vtt', 'srt')",
            name="knowledge_sources_file_type",
        ),
        sa.CheckConstraint("cardinality(sports) > 0", name="knowledge_sources_sports_not_empty"),
        sa.CheckConstraint("btrim(license_note) <> ''", name="knowledge_sources_license_note"),
        sa.ForeignKeyConstraint(["uploaded_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_hash"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index(
        "ix_knowledge_sources_uploaded_by_user_id",
        "knowledge_sources",
        ["uploaded_by_user_id"],
        unique=False,
    )
    op.create_index("ix_knowledge_sources_status", "knowledge_sources", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_knowledge_sources_status", table_name="knowledge_sources")
    op.drop_index("ix_knowledge_sources_uploaded_by_user_id", table_name="knowledge_sources")
    op.drop_table("knowledge_sources")
