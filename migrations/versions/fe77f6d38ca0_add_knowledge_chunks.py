"""add knowledge chunks

Revision ID: fe77f6d38ca0
Revises: c3a85f0d6e17
Create Date: 2026-11-04 09:00:00.000000

Expand only (ADR-0024), for #423:
- A new `knowledge_chunks` table holds each ingested source's passages, with their embedding,
  heading path, section kind, locator and weighted `tsvector` (ADR-0011, ADR-0033, ADR-0036). It
  is created empty with its indexes in this transaction, so no lock is held on live data. It has
  no `user_id` and no reference to `users`: a chunk holds no athlete data (ADR-0011 invariant 2),
  and it goes with its source by cascade.
- `knowledge_sources` gains two nullable columns, `claim_id` and `lease_expires_at`, the worker's
  claim and its heartbeat lease (research knowledge-base.md rec 10). No default, no backfill:
  every existing row reads NULL, which is "not claimed".

Release N's code never reads either, so an app rollback stays safe (invariant 4).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "fe77f6d38ca0"
down_revision: str | Sequence[str] | None = "c3a85f0d6e17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Fixed by ADR-0033: `qwen/qwen3-embedding-8b` asked for 1,024 dimensions.
_DIMENSIONS = 1024


def upgrade() -> None:
    op.add_column("knowledge_sources", sa.Column("claim_id", sa.Uuid(), nullable=True))
    op.add_column(
        "knowledge_sources",
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(_DIMENSIONS), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("sports", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "topics",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column(
            "heading_path",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("section_kind", sa.Text(), nullable=False),
        sa.Column("locator", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("exercise_id", sa.Uuid(), nullable=True),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="knowledge_chunks_ordinal"),
        sa.CheckConstraint("btrim(text) <> ''", name="knowledge_chunks_text"),
        sa.CheckConstraint("btrim(embedding_model) <> ''", name="knowledge_chunks_embedding_model"),
        sa.CheckConstraint("cardinality(sports) > 0", name="knowledge_chunks_sports_not_empty"),
        sa.CheckConstraint("language IN ('en', 'pl')", name="knowledge_chunks_language"),
        sa.CheckConstraint(
            "section_kind IN ('abstract', 'body', 'table')", name="knowledge_chunks_section_kind"
        ),
        sa.CheckConstraint(
            "locator ->> 'kind' IN ('section', 'page', 'spine', 'cue')",
            name="knowledge_chunks_locator_kind",
        ),
        sa.ForeignKeyConstraint(["source_id"], ["knowledge_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["exercise_id"], ["exercises.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id", "ordinal", name="knowledge_chunks_source_id_ordinal_key"
        ),
    )
    # pgvector's defaults (m = 16, ef_construction = 64), kept until a recall check says
    # otherwise (hybrid-search.md). A bulk load rebuilds it (#462).
    op.create_index(
        "ix_knowledge_chunks_embedding",
        "knowledge_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_knowledge_chunks_search_vector",
        "knowledge_chunks",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_knowledge_chunks_exercise_id",
        "knowledge_chunks",
        ["exercise_id"],
        postgresql_where=sa.text("exercise_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Dev and tests only; production is fixed forward, never downgraded (ADR-0024).
    op.drop_table("knowledge_chunks")
    op.drop_column("knowledge_sources", "lease_expires_at")
    op.drop_column("knowledge_sources", "claim_id")
