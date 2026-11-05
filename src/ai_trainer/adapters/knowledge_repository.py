from collections.abc import Callable, Collection, Sequence
from uuid import UUID

from sqlalchemy import delete, or_, select, update
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
from ai_trainer.domain.source_template import LicenceTerms

# What a proposal can become (#461).
_DECIDED = frozenset({SourceStatus.UPLOADED, SourceStatus.REJECTED})


def to_domain(row: KnowledgeSourceOrm) -> KnowledgeSource:
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
        license_id=None if row.license_id is None else LicenseId(row.license_id),
        license_source=None if row.license_source is None else LicenseSource(row.license_source),
        license_url=row.license_url,
        copyright_statement=row.copyright_statement,
        attribution=row.attribution,
        license_matched_line=row.license_matched_line,
        license_locator=row.license_locator,
        license_detection_reason=row.license_detection_reason,
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
        chunk_count=row.chunk_count or 0,
        embedding_model=row.embedding_model,
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
            return [to_domain(row) for row in rows]

    async def list_page(self, *, after: UUID | None, limit: int) -> Sequence[KnowledgeSource]:
        query = select(KnowledgeSourceOrm).order_by(KnowledgeSourceOrm.id).limit(limit)
        if after is not None:
            query = query.where(KnowledgeSourceOrm.id > after)
        async with self._session_factory() as session:
            return [to_domain(row) for row in await session.scalars(query)]

    async def get(self, source_id: UUID) -> KnowledgeSource | None:
        async with self._session_factory() as session:
            row = await session.get(KnowledgeSourceOrm, source_id)
            return None if row is None else to_domain(row)

    async def get_by_hash(self, content_hash: str) -> KnowledgeSource | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(KnowledgeSourceOrm).where(KnowledgeSourceOrm.content_hash == content_hash)
            )
            return None if row is None else to_domain(row)

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
            stored = to_domain(row)
            await session.commit()
            return stored

    async def find_by_identifiers(
        self,
        *,
        pmcid: str | None = None,
        doi: str | None = None,
        pmid: str | None = None,
        isbn: str | None = None,
    ) -> KnowledgeSource | None:
        matches = [
            column == value
            for column, value in (
                (KnowledgeSourceOrm.pmcid, pmcid),
                (KnowledgeSourceOrm.doi, doi),
                (KnowledgeSourceOrm.pmid, pmid),
                (KnowledgeSourceOrm.isbn, isbn),
            )
            if value is not None
        ]
        if not matches:
            return None
        async with self._session_factory() as session:
            row = await session.scalar(
                select(KnowledgeSourceOrm)
                .where(or_(*matches))
                .order_by(KnowledgeSourceOrm.uploaded_at, KnowledgeSourceOrm.id)
                .limit(1)
            )
            return None if row is None else to_domain(row)

    async def add_if_new(self, source: NewKnowledgeSource) -> KnowledgeSource | None:
        values = source.model_dump(mode="python")
        values["sports"], values["topics"] = list(source.sports), list(source.topics)
        # ON CONFLICT DO NOTHING on any unique key: hash or an identifier.
        statement = (
            insert(KnowledgeSourceOrm)
            .values(values)
            .on_conflict_do_nothing()
            .returning(KnowledgeSourceOrm)
        )
        async with self._session_factory() as session:
            row = await session.scalar(statement)
            stored = None if row is None else to_domain(row)
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
            queued = None if row is None else to_domain(row)
            await session.commit()
            return queued

    async def get_many(self, source_ids: Collection[UUID]) -> Sequence[KnowledgeSource]:
        if not source_ids:
            return []
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(KnowledgeSourceOrm).where(KnowledgeSourceOrm.id.in_(list(source_ids)))
            )
            return [to_domain(row) for row in rows]

    async def decide_proposals(
        self, source_ids: Collection[UUID], *, status: SourceStatus, decided_by: UUID
    ) -> Sequence[UUID]:
        if status not in _DECIDED:
            raise ValueError(f"a proposal becomes uploaded or rejected, not {status}")
        if not source_ids:
            return []
        # Approving makes the admin the acting user of the source's embedding calls (ADR-0036).
        approver = {"uploaded_by_user_id": decided_by} if status is SourceStatus.UPLOADED else {}
        statement = (
            update(KnowledgeSourceOrm)
            .where(
                KnowledgeSourceOrm.id.in_(list(source_ids)),
                KnowledgeSourceOrm.status == SourceStatus.PROPOSED.value,
                # No licence, no leaving `proposed` (ADR-0036 invariant 4, `licence_set`).
                KnowledgeSourceOrm.license_id.is_not(None),
            )
            .values(status=status.value, **approver)
            .returning(KnowledgeSourceOrm.id)
        )
        async with self._session_factory() as session:
            changed = list(await session.scalars(statement))
            await session.commit()
            return changed

    async def set_licence(self, source_id: UUID, terms: LicenceTerms) -> KnowledgeSource | None:
        statement = (
            update(KnowledgeSourceOrm)
            .where(
                KnowledgeSourceOrm.id == source_id,
                KnowledgeSourceOrm.status == SourceStatus.PROPOSED.value,
            )
            .values(
                license_id=terms.license_id.value,
                license_source=terms.license_source.value,
                license_url=terms.license_url,
                copyright_statement=terms.copyright_statement,
                attribution=terms.attribution,
                license_note=terms.license_note,
                # What detection found no longer describes the licence: the admin set it.
                license_matched_line=None,
                license_locator=None,
                license_detection_reason=None,
            )
            .returning(KnowledgeSourceOrm)
        )
        async with self._session_factory() as session:
            row = await session.scalar(statement)
            updated = None if row is None else to_domain(row)
            await session.commit()
            return updated
