import uuid
from datetime import datetime

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from ai_trainer.adapters.orm.base import Base
from ai_trainer.domain.knowledge_document import EMBEDDING_DIMENSIONS


class KnowledgeSourceOrm(Base):
    """The knowledge base's manifest, one row per source (ADR-0011, ADR-0033). Shared reference
    data: it holds no athlete data, and it outlives the admin who uploaded it."""

    __tablename__ = "knowledge_sources"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('research_paper', 'book', 'talk_or_podcast', 'article', 'catalogue')",
            name="knowledge_sources_source_type",
        ),
        CheckConstraint(
            "evidence_tier IN ('peer_reviewed', 'coaching_book', 'article_or_transcript', "
            "'catalogue')",
            name="knowledge_sources_evidence_tier",
        ),
        CheckConstraint("language IN ('en', 'pl')", name="knowledge_sources_language"),
        CheckConstraint(
            "status IN ('detecting', 'proposed', 'rejected', 'uploaded', 'ingesting', 'ingested', "
            "'failed')",
            name="knowledge_sources_status",
        ),
        CheckConstraint(
            "file_type IN ('pdf', 'epub', 'markdown', 'text', 'vtt', 'srt', 'jats')",
            name="knowledge_sources_file_type",
        ),
        CheckConstraint("cardinality(sports) > 0", name="knowledge_sources_sports_not_empty"),
        # A note, when there is one, says something; `other` needs one (ADR-0036).
        CheckConstraint("btrim(license_note) <> ''", name="knowledge_sources_license_note"),
        CheckConstraint(
            "license_id IN ('cc0', 'cc-by', 'cc-by-sa', 'cc-by-nc', 'cc-by-nc-sa', 'cc-by-nd', "
            "'cc-by-nc-nd', 'other')",
            name="knowledge_sources_license_id",
        ),
        CheckConstraint(
            "license_source IN ('pmc', 'jats', 'admin', 'file')",
            name="knowledge_sources_license_source",
        ),
        # A source waits without a licence only while it is detected or proposed (the #454
        # amendment of ADR-0036); otherwise both fields are set, and always together.
        CheckConstraint(
            "(license_id IS NULL) = (license_source IS NULL)",
            name="knowledge_sources_licence_pair",
        ),
        CheckConstraint(
            "status IN ('detecting', 'proposed') OR license_id IS NOT NULL",
            name="knowledge_sources_licence_set",
        ),
        CheckConstraint(
            "license_id <> 'other' OR license_note IS NOT NULL",
            name="knowledge_sources_other_license_note",
        ),
        CheckConstraint("pmcid ~ '^PMC[0-9]+$'", name="knowledge_sources_pmcid_format"),
        CheckConstraint("pmid ~ '^[0-9]+$'", name="knowledge_sources_pmid_format"),
        # A bare DOI, lowercased: DOIs are case-insensitive, so the unique key catches any case.
        CheckConstraint(
            "doi ~ '^10\\.[0-9.]+/.' AND doi = lower(doi)", name="knowledge_sources_doi_format"
        ),
        Index("ix_knowledge_sources_uploaded_by_user_id", "uploaded_by_user_id"),
        Index("ix_knowledge_sources_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(nullable=False)
    evidence_tier: Mapped[str] = mapped_column(nullable=False)
    language: Mapped[str] = mapped_column(nullable=False)
    sports: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    topics: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    license_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ADR-0036's structured licence; #422's rows are `other`, set by an admin. NULL while it
    # is detected from the file or awaits an admin, written as an explicit NULL.
    license_id: Mapped[str | None] = mapped_column(nullable=True, server_default="other")
    license_source: Mapped[str | None] = mapped_column(nullable=True, server_default="admin")
    license_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    copyright_statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    attribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What detection found: the matched line, its page or locator, or why no licence was set.
    license_matched_line: Mapped[str | None] = mapped_column(Text, nullable=True)
    license_locator: Mapped[str | None] = mapped_column(Text, nullable=True)
    license_detection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Bibliographic identifiers, each unique when set.
    pmcid: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    doi: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    pmid: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    isbn: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SHA-256 of the file's bytes, hex: the same file is one source (ADR-0011). A paper staged by
    # PMC ID holds the hash of `pmc:<PMCID>` until its JATS is fetched (ADR-0036, #458).
    content_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # NULL for the catalogue's rows, which have no uploaded file (#427).
    file_type: Mapped[str | None] = mapped_column(nullable=True)
    file_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    status: Mapped[str] = mapped_column(nullable=False, server_default="uploaded")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The admin who uploaded it or last asked for re-ingest, the acting user of its embedding
    # calls (ADR-0018). A source outlives that account.
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # timestamptz, UTC (ADR-0004 invariant 5).
    uploaded_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    ingested_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    # The worker's claim on an `ingesting` source and its heartbeat lease (#423). A source whose
    # lease has lapsed is claimed again; one whose claim changed is no longer this worker's.
    claim_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # What the last ingest stored, written with its chunks, for the admin list (#423).
    chunk_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Takeovers of a lapsed lease since the last claim from `uploaded` (#462).
    ingest_takeovers: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )


class KnowledgeChunkOrm(Base):
    """One passage of an ingested source, with its embedding and full-text vector (ADR-0011,
    ADR-0033, ADR-0036). Shared reference data: no `user_id`, nothing about an athlete, and it
    goes with its source."""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="knowledge_chunks_ordinal"),
        CheckConstraint("btrim(text) <> ''", name="knowledge_chunks_text"),
        CheckConstraint("btrim(embedding_model) <> ''", name="knowledge_chunks_embedding_model"),
        CheckConstraint("cardinality(sports) > 0", name="knowledge_chunks_sports_not_empty"),
        CheckConstraint("language IN ('en', 'pl')", name="knowledge_chunks_language"),
        CheckConstraint(
            "section_kind IN ('abstract', 'body', 'table')", name="knowledge_chunks_section_kind"
        ),
        CheckConstraint(
            "locator ->> 'kind' IN ('section', 'page', 'spine', 'cue')",
            name="knowledge_chunks_locator_kind",
        ),
        UniqueConstraint("source_id", "ordinal", name="knowledge_chunks_source_id_ordinal_key"),
        Index(
            "ix_knowledge_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_knowledge_chunks_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_knowledge_chunks_exercise_id",
            "exercise_id",
            postgresql_where=sa.text("exercise_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"), nullable=False
    )
    # The chunk's place in its source, from 0.
    ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    # Chunks from different models are never searched together (ADR-0011 invariant 4).
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    # Copied from the source, so a search filters chunks alone.
    sports: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    topics: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=sa.text("'{}'::text[]")
    )
    language: Mapped[str] = mapped_column(Text, nullable=False)
    heading_path: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=sa.text("'{}'::text[]")
    )
    section_kind: Mapped[str] = mapped_column(Text, nullable=False)
    # A section, page, spine item or cue, by `kind` (`domain/knowledge_document.Locator`).
    locator: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    # A catalogue exercise's text (#427); NULL for every uploaded source's chunk.
    exercise_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("exercises.id", ondelete="CASCADE"), nullable=True
    )
    # Title A, heading path B, text D, in `english` or `simple` by language (ADR-0033, ADR-0036);
    # written by the insert, since a generated column can't read the source's title.
    search_vector: Mapped[str] = mapped_column(TSVECTOR, nullable=False)
    # timestamptz, UTC (ADR-0004 invariant 5).
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class AdminTokenOrm(Base):
    """An admin's staging token (ADR-0036): stored only as its SHA-256, with a display prefix,
    expiring within 30 days. Owned by its admin and gone with the account (G7)."""

    __tablename__ = "admin_tokens"
    __table_args__ = (
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="admin_tokens_token_hash"),
        CheckConstraint("btrim(display_prefix) <> ''", name="admin_tokens_display_prefix"),
        CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + interval '30 days'",
            name="admin_tokens_expires_at",
        ),
        UniqueConstraint("token_hash", name="admin_tokens_token_hash_key"),
        Index("ix_admin_tokens_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    # timestamptz, UTC (ADR-0004 invariant 5).
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
