import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
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
            "status IN ('uploaded', 'ingesting', 'ingested', 'failed')",
            name="knowledge_sources_status",
        ),
        CheckConstraint(
            "file_type IN ('pdf', 'epub', 'markdown', 'text', 'vtt', 'srt')",
            name="knowledge_sources_file_type",
        ),
        CheckConstraint("cardinality(sports) > 0", name="knowledge_sources_sports_not_empty"),
        # Invariant 5: no source without a tier and a license note.
        CheckConstraint("btrim(license_note) <> ''", name="knowledge_sources_license_note"),
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
    license_note: Mapped[str] = mapped_column(Text, nullable=False)
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
