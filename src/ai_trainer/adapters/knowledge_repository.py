from collections.abc import Callable, Sequence
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import KnowledgeSourceOrm
from ai_trainer.domain.knowledge import (
    REINGESTABLE,
    DuplicateSourceError,
    EvidenceTier,
    KnowledgeFileType,
    KnowledgeSource,
    LicenseId,
    LicenseSource,
    NewKnowledgeSource,
    SourceLanguage,
    SourceStatus,
    SourceType,
)


def _to_domain(row: KnowledgeSourceOrm) -> KnowledgeSource:
    return KnowledgeSource(
        id=row.id,
        title=row.title,
        author=row.author,
        source_type=SourceType(row.source_type),
        evidence_tier=EvidenceTier(row.evidence_tier),
        language=SourceLanguage(row.language),
        sports=tuple(row.sports),
        topics=tuple(row.topics),
        license_note=row.license_note,
        license_id=LicenseId(row.license_id),
        license_source=LicenseSource(row.license_source),
        license_url=row.license_url,
        copyright_statement=row.copyright_statement,
        attribution=row.attribution,
        pmcid=row.pmcid,
        doi=row.doi,
        pmid=row.pmid,
        isbn=row.isbn,
        source_url=row.source_url,
        content_hash=row.content_hash,
        file_type=None if row.file_type is None else KnowledgeFileType(row.file_type),
        file_name=row.file_name,
        size_bytes=row.size_bytes,
        storage_key=row.storage_key,
        status=SourceStatus(row.status),
        error=row.error,
        uploaded_by_user_id=row.uploaded_by_user_id,
        uploaded_at=row.uploaded_at,
        ingested_at=row.ingested_at,
    )


class SqlAlchemyKnowledgeSourcesRepository:
    """Implements `KnowledgeSourcesRepositoryPort` (ADR-0033)."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_all(self) -> Sequence[KnowledgeSource]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(KnowledgeSourceOrm).order_by(
                    KnowledgeSourceOrm.uploaded_at.desc(), KnowledgeSourceOrm.id
                )
            )
            return [_to_domain(row) for row in rows]

    async def get(self, source_id: UUID) -> KnowledgeSource | None:
        async with self._session_factory() as session:
            row = await session.get(KnowledgeSourceOrm, source_id)
            return None if row is None else _to_domain(row)

    async def get_by_hash(self, content_hash: str) -> KnowledgeSource | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(KnowledgeSourceOrm).where(KnowledgeSourceOrm.content_hash == content_hash)
            )
            return None if row is None else _to_domain(row)

    async def add(self, source: NewKnowledgeSource) -> KnowledgeSource:
        values = source.model_dump(mode="python")
        values["sports"], values["topics"] = list(source.sports), list(source.topics)
        # ON CONFLICT DO NOTHING on the hash, so a racing upload of the same file loses quietly
        # instead of aborting the transaction (research knowledge-base.md rec 9).
        statement = (
            insert(KnowledgeSourceOrm)
            .values(values)
            .on_conflict_do_nothing(index_elements=[KnowledgeSourceOrm.content_hash])
            .returning(KnowledgeSourceOrm)
        )
        async with self._session_factory() as session:
            row = await session.scalar(statement)
            if row is None:
                raise DuplicateSourceError
            stored = _to_domain(row)
            await session.commit()
            return stored

    async def delete(self, source_id: UUID) -> bool:
        async with self._session_factory() as session:
            deleted = await session.scalar(
                delete(KnowledgeSourceOrm)
                .where(KnowledgeSourceOrm.id == source_id)
                .returning(KnowledgeSourceOrm.id)
            )
            await session.commit()
            return deleted is not None

    async def queue_reingest(
        self, source_id: UUID, *, requested_by: UUID
    ) -> KnowledgeSource | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                update(KnowledgeSourceOrm)
                .where(
                    KnowledgeSourceOrm.id == source_id,
                    KnowledgeSourceOrm.status.in_([s.value for s in REINGESTABLE]),
                )
                .values(
                    status=SourceStatus.UPLOADED.value,
                    error=None,
                    ingested_at=None,
                    uploaded_by_user_id=requested_by,
                )
                .returning(KnowledgeSourceOrm)
            )
            queued = None if row is None else _to_domain(row)
            await session.commit()
            return queued
