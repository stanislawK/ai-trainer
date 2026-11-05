"""The exercise catalogue's knowledge sources and their chunks on PostgreSQL (ADR-0033 *Catalogue
texts*, ADR-0036's #427 amendment).

One source per language, keyed by `catalogue_content_hash`. Its chunks are written batch by
batch under the worker's claim. A replaced chunk keeps its ordinal, and a new one takes the next
free number. A catalogue chunk's `tsvector` weights the exercise name A and the text D, in
`english` or `simple` (ADR-0033); the source title, the same in every chunk, would add nothing.
"""

from collections.abc import Callable, Sequence
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, insert, or_, select
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.knowledge_chunk_repository import held, search_vector
from ai_trainer.adapters.orm import (
    ExerciseOrm,
    ExerciseTextOrm,
    KnowledgeChunkOrm,
    KnowledgeSourceOrm,
    UserOrm,
)
from ai_trainer.domain.catalogue_knowledge import (
    CATALOGUE_LICENSE_NOTE,
    CATALOGUE_TITLE,
    CatalogueChunkState,
    CatalogueText,
    EmbeddedCatalogueChunk,
    catalogue_content_hash,
)
from ai_trainer.domain.knowledge import (
    EvidenceTier,
    LicenseId,
    LicenseSource,
    SourceLanguage,
    SourceStatus,
    SourceType,
)
from ai_trainer.domain.users import UserStatus

_C = KnowledgeChunkOrm
_S = KnowledgeSourceOrm
_CATALOGUE_SPORTS = ["gym"]
# `uploaded` is waiting already; the others are queued again when their texts are stale.
_REQUEUEABLE = (
    SourceStatus.INGESTING.value,
    SourceStatus.INGESTED.value,
    SourceStatus.FAILED.value,
)
# Python's `str.strip()`, as `catalogue_chunk` trims a text, so a stored chunk compares equal.
_WHITESPACE = r"^\s+|\s+$"


class SqlAlchemyCatalogueSources:
    """Implements `CatalogueSourcesPort`."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def queue(
        self,
        *,
        attribution: str | None,
        admin_emails: Sequence[str],
        now: datetime,
    ) -> int:
        async with self._session_factory() as session:
            admin = await _acting_admin(session, admin_emails)
            queued = 0
            for language in SourceLanguage:
                if await _add(session, language, attribution, admin, now):
                    queued += 1
                    continue
                source = await session.scalar(
                    select(_S)
                    .where(_S.content_hash == catalogue_content_hash(language))
                    .with_for_update()
                )
                if source is None or source.status not in _REQUEUEABLE:
                    continue
                if source.status == SourceStatus.FAILED.value or await _stale(
                    session, language, source.id
                ):
                    _requeue(source, attribution, admin, now)
                    queued += 1
            await session.commit()
            return queued


async def _stale(session: AsyncSession, language: SourceLanguage, source_id: UUID) -> bool:
    """Whether any catalogue text in `language` has no chunk, or one whose text or name
    differs. It reads the database, not what this import wrote, so an import that committed its
    rows but failed before queueing is caught by the next one. A model change is a re-embed job
    (ADR-0033 invariant 4), started by Re-ingest."""
    instructions = func.regexp_replace(ExerciseTextOrm.instructions, _WHITESPACE, "", "g")
    statement = (
        select(ExerciseTextOrm.exercise_id)
        .join(ExerciseOrm, ExerciseOrm.id == ExerciseTextOrm.exercise_id)
        .outerjoin(
            _C, and_(_C.source_id == source_id, _C.exercise_id == ExerciseTextOrm.exercise_id)
        )
        .where(
            ExerciseOrm.owner_user_id.is_(None),
            ExerciseTextOrm.lang == language.value,
            instructions != "",
            or_(
                _C.id.is_(None),
                _C.text.is_distinct_from(instructions),
                _C.heading_path.is_distinct_from(array([ExerciseOrm.name])),
            ),
        )
        .limit(1)
    )
    return await session.scalar(statement) is not None


async def _acting_admin(session: AsyncSession, admin_emails: Sequence[str]) -> UUID | None:
    """The oldest active user named in `ADMIN_EMAILS`, compared case-insensitively as the admin
    check is (ADR-0005); admin rights still come only from the setting."""
    if not admin_emails:
        return None
    admin: UUID | None = await session.scalar(
        select(UserOrm.id)
        .where(
            func.lower(UserOrm.email).in_([email.lower() for email in admin_emails]),
            UserOrm.status == UserStatus.ACTIVE.value,
        )
        .order_by(UserOrm.created_at, UserOrm.id)
        .limit(1)
    )
    return admin


async def _add(
    session: AsyncSession,
    language: SourceLanguage,
    attribution: str | None,
    admin: UUID | None,
    now: datetime,
) -> bool:
    statement = (
        pg_insert(_S)
        .values(
            id=uuid4(),
            title=CATALOGUE_TITLE,
            author=None,
            source_type=SourceType.CATALOGUE.value,
            evidence_tier=EvidenceTier.CATALOGUE.value,
            language=language.value,
            sports=_CATALOGUE_SPORTS,
            license_note=CATALOGUE_LICENSE_NOTE,
            license_id=LicenseId.OTHER.value,
            license_source=LicenseSource.ADMIN.value,
            attribution=attribution,
            content_hash=catalogue_content_hash(language),
            status=SourceStatus.UPLOADED.value,
            uploaded_by_user_id=admin,
            uploaded_at=now,
        )
        .on_conflict_do_nothing(index_elements=["content_hash"])
        .returning(_S.id)
    )
    return await session.scalar(statement) is not None


def _requeue(
    source: KnowledgeSourceOrm, attribution: str | None, admin: UUID | None, now: datetime
) -> None:
    """Back to `uploaded`. An `ingesting` worker's later writes name a status this clears, so
    they change nothing, and the next claim works out what changed again."""
    source.status = SourceStatus.UPLOADED.value
    source.uploaded_by_user_id = admin
    source.uploaded_at = now
    source.attribution = attribution
    source.error = None
    source.ingested_at = None
    source.claim_id = None
    source.lease_expires_at = None


class SqlAlchemyCatalogueChunks:
    """Implements `CatalogueChunksRepositoryPort`."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def texts(self, language: SourceLanguage) -> Sequence[CatalogueText]:
        statement = (
            select(ExerciseTextOrm.exercise_id, ExerciseOrm.name, ExerciseTextOrm.instructions)
            .join(ExerciseOrm, ExerciseOrm.id == ExerciseTextOrm.exercise_id)
            # Shared catalogue rows only: an athlete's custom exercise is never knowledge.
            .where(ExerciseOrm.owner_user_id.is_(None), ExerciseTextOrm.lang == language.value)
            .order_by(ExerciseOrm.name, ExerciseOrm.id)
        )
        async with self._session_factory() as session:
            rows = await session.execute(statement)
            return [CatalogueText(row[0], row[1], row[2]) for row in rows]

    async def chunk_states(self, source_id: UUID) -> Sequence[CatalogueChunkState]:
        statement = select(_C.exercise_id, _C.text, _C.heading_path, _C.embedding_model).where(
            _C.source_id == source_id, _C.exercise_id.is_not(None)
        )
        async with self._session_factory() as session:
            rows = await session.execute(statement)
            return [
                CatalogueChunkState(row[0], row[1], tuple(row[2]), row[3])
                for row in rows
                if row[0] is not None
            ]

    async def write_batch(
        self,
        source_id: UUID,
        *,
        claim_id: UUID,
        chunks: Sequence[EmbeddedCatalogueChunk],
        model: str,
        at: datetime,
    ) -> bool:
        ids = [embedded.item.exercise_id for embedded in chunks]
        async with self._session_factory() as session:
            source = await session.scalar(
                select(_S).where(held(source_id, claim_id)).with_for_update()
            )
            if source is None:
                return False
            stored = await session.execute(
                select(_C.exercise_id, _C.ordinal).where(
                    _C.source_id == source_id, _C.exercise_id.in_(ids)
                )
            )
            ordinals = {exercise_id: ordinal for exercise_id, ordinal in stored}
            last = await session.scalar(
                select(func.coalesce(func.max(_C.ordinal), -1)).where(_C.source_id == source_id)
            )
            next_ordinal = int(last if last is not None else -1) + 1
            await session.execute(
                delete(_C).where(_C.source_id == source_id, _C.exercise_id.in_(ids))
            )
            rows: list[dict[str, object]] = []
            for embedded in chunks:
                ordinal = ordinals.get(embedded.item.exercise_id)
                if ordinal is None:
                    ordinal, next_ordinal = next_ordinal, next_ordinal + 1
                rows.append(_row(source, embedded, ordinal=ordinal, model=model, at=at))
            if rows:
                await session.execute(insert(_C).values(rows))
            # The model is the source's only once `finish` has every chunk on it.
            source.chunk_count = await _count(session, source_id)
            await session.commit()
            return True

    async def finish(
        self,
        source_id: UUID,
        *,
        claim_id: UUID,
        keep: frozenset[UUID],
        model: str,
        ingested_at: datetime,
    ) -> bool:
        async with self._session_factory() as session:
            source = await session.scalar(
                select(_S).where(held(source_id, claim_id)).with_for_update()
            )
            if source is None:
                return False
            await session.execute(
                delete(_C).where(
                    _C.source_id == source_id,
                    _C.exercise_id.is_(None) | _C.exercise_id.not_in(keep),
                )
            )
            source.status = SourceStatus.INGESTED.value
            source.ingested_at = ingested_at
            source.chunk_count = await _count(session, source_id)
            source.embedding_model = model
            source.error = None
            source.claim_id = None
            source.lease_expires_at = None
            await session.commit()
            return True


async def _count(session: AsyncSession, source_id: UUID) -> int:
    count = await session.scalar(
        select(func.count()).select_from(_C).where(_C.source_id == source_id)
    )
    return int(count or 0)


def _row(
    source: KnowledgeSourceOrm,
    embedded: EmbeddedCatalogueChunk,
    *,
    ordinal: int,
    model: str,
    at: datetime,
) -> dict[str, object]:
    chunk = embedded.item.chunk
    language = SourceLanguage(source.language)
    return {
        "id": uuid4(),
        "source_id": source.id,
        "ordinal": ordinal,
        "text": chunk.text,
        "embedding": list(embedded.embedding),
        "embedding_model": model,
        "sports": list(source.sports),
        "topics": list(source.topics),
        "language": language.value,
        "heading_path": list(chunk.heading_path),
        "section_kind": chunk.section_kind.value,
        "locator": chunk.locator.model_dump(mode="json"),
        "exercise_id": embedded.item.exercise_id,
        "search_vector": search_vector(
            language=language, title=" ".join(chunk.heading_path), headings=(), body=chunk.text
        ),
        "created_at": at,
    }
