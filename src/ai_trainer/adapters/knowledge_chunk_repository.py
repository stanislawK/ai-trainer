"""The ingestion worker's queue and its chunk writes on PostgreSQL (ADR-0036, #423).

The queue is the sources' status row: `FOR UPDATE SKIP LOCKED` keeps two workers off one
source, and a `claim_id` with a lease lets a later worker take over a source whose worker died
(knowledge-base.md rec 10). Every write after the claim names the claim, so a worker that lost
it, or whose source was deleted, writes nothing.

A chunk's `tsvector` weights the source's title A, the heading path B and the text D
(ADR-0036), in `english` for English and `simple` for Polish, which has no stemmer (ADR-0033).
It is computed in the INSERT, since a generated column can't read the source's title.
"""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import (
    ColumnElement,
    and_,
    cast,
    delete,
    func,
    insert,
    literal,
    literal_column,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import REGCONFIG, TSVECTOR
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.knowledge_repository import to_domain
from ai_trainer.adapters.orm import KnowledgeChunkOrm, KnowledgeSourceOrm
from ai_trainer.domain.knowledge import KnowledgeSource, SourceLanguage, SourceStatus
from ai_trainer.domain.knowledge_ingestion import EmbeddedChunk, IngestFailure

_TEXT_SEARCH_CONFIGS = {SourceLanguage.EN: "english", SourceLanguage.PL: "simple"}
# Rows per INSERT, so a long book's chunks don't make one enormous statement.
_INSERT_ROWS = 100

_S = KnowledgeSourceOrm


def _weighted(config: str, part: str, weight: Literal["A", "B", "D"]) -> ColumnElement[Any]:
    vector = func.to_tsvector(cast(literal(config), REGCONFIG), part)
    # `setweight` takes a `"char"`, which a bound string isn't; the weight is one of three
    # constants, so it is written into the SQL.
    return func.setweight(vector, literal_column(f"'{weight}'"), type_=TSVECTOR)


def search_vector(
    *, language: SourceLanguage, title: str, headings: Sequence[str], body: str
) -> ColumnElement[Any]:
    config = _TEXT_SEARCH_CONFIGS[language]
    return (
        _weighted(config, title, "A")
        .op("||", return_type=TSVECTOR)(_weighted(config, " ".join(headings), "B"))
        .op("||", return_type=TSVECTOR)(_weighted(config, body, "D"))
    )


def _held(source_id: UUID, claim_id: UUID) -> ColumnElement[bool]:
    return and_(
        _S.id == source_id,
        _S.claim_id == claim_id,
        _S.status == SourceStatus.INGESTING.value,
    )


class SqlAlchemyKnowledgeIngestionRepository:
    """Implements `KnowledgeIngestionRepositoryPort`."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim_next(
        self, *, claim_id: UUID, now: datetime, lease_until: datetime
    ) -> KnowledgeSource | None:
        waiting = (
            select(_S.id)
            .where(
                or_(
                    _S.status == SourceStatus.UPLOADED.value,
                    # A lapsed lease, or none at all: an `ingesting` row no worker leases.
                    and_(
                        _S.status == SourceStatus.INGESTING.value,
                        or_(_S.lease_expires_at.is_(None), _S.lease_expires_at < now),
                    ),
                )
            )
            .order_by(_S.uploaded_at, _S.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        statement = (
            update(_S)
            .where(_S.id == waiting)
            .values(
                status=SourceStatus.INGESTING.value,
                claim_id=claim_id,
                lease_expires_at=lease_until,
            )
            .returning(_S)
        )
        async with self._session_factory() as session:
            row = await session.scalar(statement)
            claimed = None if row is None else to_domain(row)
            await session.commit()
            return claimed

    async def heartbeat(self, source_id: UUID, *, claim_id: UUID, lease_until: datetime) -> bool:
        statement = (
            update(_S)
            .where(_held(source_id, claim_id))
            .values(lease_expires_at=lease_until)
            .returning(_S.id)
        )
        async with self._session_factory() as session:
            renewed = await session.scalar(statement)
            await session.commit()
            return renewed is not None

    async def finish(
        self,
        source_id: UUID,
        *,
        claim_id: UUID,
        chunks: Sequence[EmbeddedChunk],
        model: str,
        ingested_at: datetime,
    ) -> bool:
        async with self._session_factory() as session:
            # The row lock holds off a delete until this commits; the delete then removes these
            # chunks with the source, by cascade.
            source = await session.scalar(
                select(_S).where(_held(source_id, claim_id)).with_for_update()
            )
            if source is None:
                return False
            await session.execute(
                delete(KnowledgeChunkOrm).where(KnowledgeChunkOrm.source_id == source_id)
            )
            rows = [self._row(source, chunk, model, ingested_at) for chunk in chunks]
            for start in range(0, len(rows), _INSERT_ROWS):
                await session.execute(
                    insert(KnowledgeChunkOrm).values(rows[start : start + _INSERT_ROWS])
                )
            source.status = SourceStatus.INGESTED.value
            source.ingested_at = ingested_at
            source.chunk_count = len(rows)
            source.embedding_model = model
            source.error = None
            source.claim_id = None
            source.lease_expires_at = None
            await session.commit()
            return True

    @staticmethod
    def _row(
        source: KnowledgeSourceOrm, embedded: EmbeddedChunk, model: str, at: datetime
    ) -> dict[str, object]:
        chunk = embedded.chunk
        language = SourceLanguage(source.language)
        return {
            "id": uuid4(),
            "source_id": source.id,
            "ordinal": chunk.ordinal,
            "text": chunk.text,
            "embedding": list(embedded.embedding),
            "embedding_model": model,
            "sports": list(source.sports),
            "topics": list(source.topics),
            "language": language.value,
            "heading_path": list(chunk.heading_path),
            "section_kind": chunk.section_kind.value,
            "locator": chunk.locator.model_dump(mode="json"),
            "search_vector": search_vector(
                language=language,
                title=source.title,
                headings=chunk.heading_path,
                body=chunk.text,
            ),
            "created_at": at,
        }

    async def fail(self, source_id: UUID, *, claim_id: UUID, reason: IngestFailure) -> bool:
        statement = (
            update(_S)
            .where(_held(source_id, claim_id))
            .values(
                status=SourceStatus.FAILED.value,
                error=reason.value,
                claim_id=None,
                lease_expires_at=None,
            )
            .returning(_S.id)
        )
        async with self._session_factory() as session:
            failed = await session.scalar(statement)
            await session.commit()
            return failed is not None
