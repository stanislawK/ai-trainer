import uuid
from datetime import datetime

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
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from ai_trainer.adapters.orm.base import Base


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
            "status IN ('proposed', 'rejected', 'uploaded', 'ingesting', 'ingested', 'failed')",
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
            "license_source IN ('pmc', 'jats', 'admin')", name="knowledge_sources_license_source"
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
    # ADR-0036's structured licence; #422's rows are `other`, set by an admin.
    license_id: Mapped[str] = mapped_column(nullable=False, server_default="other")
    license_source: Mapped[str] = mapped_column(nullable=False, server_default="admin")
    license_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    copyright_statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    attribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Bibliographic identifiers, each unique when set.
    pmcid: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    doi: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    pmid: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    isbn: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SHA-256 of the file's bytes, hex: the same file is one source (ADR-0011).
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
