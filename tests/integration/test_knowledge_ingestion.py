"""The ingestion worker on real PostgreSQL (ADR-0036, PRD B7, #423): one test per acceptance
criterion, with the object storage and the embedding gateway faked at their ports.

Sources here are dated 2001, so a claim, which takes the oldest waiting source, takes this test's
source before anything committed: the worktree's dev data, or `test_ingest_worker_cli`'s 2002
source. Each test claims once (`ingest_once`), never until the queue is empty.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.knowledge_chunk_repository import SqlAlchemyKnowledgeIngestionRepository
from ai_trainer.adapters.knowledge_repository import SqlAlchemyKnowledgeSourcesRepository
from ai_trainer.adapters.orm import KnowledgeChunkOrm
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.knowledge_ingestion import KnowledgeIngestion
from ai_trainer.application.knowledge_sources import KnowledgeSources, ReingestQueued
from ai_trainer.domain.embeddings import EmbeddingFailed, EmbeddingFailure
from ai_trainer.domain.knowledge import (
    KnowledgeFileType,
    KnowledgeSource,
    SourceLanguage,
    SourceStatus,
)
from ai_trainer.domain.knowledge_document import SectionKind
from ai_trainer.domain.knowledge_ingestion import IngestReport
from ai_trainer.domain.users import NewUser, User, UserStatus
from tests.ingestion_support import (
    DIMENSIONS,
    MODEL,
    FrozenClock,
    ScriptedEmbeddings,
    guide_markdown,
    ingestion,
    uploaded_source,
)
from tests.knowledge_support import MemoryStorage
from tests.pdf_support import blank_pdf, encrypted_pdf, text_pdf

_LONG_AGO = datetime(2001, 1, 1, tzinfo=UTC)
_LEASE = timedelta(minutes=5)

type Factory = Callable[[], AsyncSession]


class _CountingStorage(MemoryStorage):
    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []

    async def get(self, key: str) -> bytes:
        self.reads.append(key)
        return await super().get(key)


class _World:
    def __init__(self, factory: Factory, admin: User) -> None:
        self.factory = factory
        self.admin = admin
        self.storage = _CountingStorage()
        self.clock = FrozenClock(_LONG_AGO + timedelta(days=1))
        self.sources = SqlAlchemyKnowledgeSourcesRepository(factory)
        self.queue = SqlAlchemyKnowledgeIngestionRepository(factory)
        self.service = KnowledgeSources(
            sources=self.sources,
            storage=self.storage,
            clock=self.clock,
            max_upload_bytes=1024 * 1024,
            sports=["gym"],
        )

    async def add(
        self,
        marker: str,
        *,
        data: bytes | None = None,
        file_type: KnowledgeFileType = KnowledgeFileType.MARKDOWN,
        status: SourceStatus = SourceStatus.UPLOADED,
        language: SourceLanguage = SourceLanguage.EN,
        minutes: int = 0,
    ) -> KnowledgeSource:
        new = uploaded_source(
            admin_id=self.admin.id,
            marker=marker,
            file_type=file_type,
            status=status,
            language=language,
            uploaded_at=_LONG_AGO + timedelta(minutes=minutes),
        )
        assert new.storage_key is not None
        self.storage.objects[new.storage_key] = guide_markdown(marker) if data is None else data
        return await self.sources.add(new)

    def worker(self, embeddings: ScriptedEmbeddings | None = None) -> KnowledgeIngestion:
        return ingestion(
            repository=self.queue,
            storage=self.storage,
            embeddings=embeddings or ScriptedEmbeddings(),
            clock=self.clock,
            lease=_LEASE,
        )

    async def ingest_once(self, embeddings: ScriptedEmbeddings | None = None) -> list[IngestReport]:
        """One claim, as a list of zero or one report. Not `drain()`: that would go on to any
        committed source the database holds, a concurrent test's or the dev data's."""
        report = await self.worker(embeddings).ingest_next()
        return [] if report is None else [report]

    async def chunks(self, source_id: UUID) -> list[KnowledgeChunkOrm]:
        async with self.factory() as session:
            rows = await session.scalars(
                select(KnowledgeChunkOrm)
                .where(KnowledgeChunkOrm.source_id == source_id)
                .order_by(KnowledgeChunkOrm.ordinal)
            )
            return list(rows)

    async def status(self, source_id: UUID) -> tuple[SourceStatus, str | None] | None:
        source = await self.sources.get(source_id)
        return None if source is None else (source.status, source.error)


@pytest.fixture
async def world(db_session_factory: Factory) -> _World:
    admin = await SqlAlchemyUsersRepository(db_session_factory).create(
        NewUser(
            sub="ingest-admin",
            email="ingest-admin@example.com",
            name="Admin",
            locale="en",
            status=UserStatus.ACTIVE,
        )
    )
    return _World(db_session_factory, admin)


async def test_uploaded_markdown_reaches_ingested_with_its_chunk_count(world: _World) -> None:
    source = await world.add("ac1")

    reports = await world.ingest_once()

    assert reports == [IngestReport(source_id=source.id, result="ingested", chunks=3)]
    listed = next(s for s in await world.sources.list_all() if s.id == source.id)
    assert listed.status is SourceStatus.INGESTED
    assert listed.ingested_at == world.clock.now()
    assert (listed.chunk_count, listed.embedding_model) == (3, MODEL)
    assert len(await world.chunks(source.id)) == 3


async def test_a_worker_killed_mid_ingest_finishes_on_restart_without_duplicates(
    world: _World,
) -> None:
    source = await world.add("ac2-killed")
    inside = asyncio.Event()

    async def _hang() -> None:
        inside.set()
        await asyncio.Event().wait()

    killed = asyncio.create_task(world.worker(ScriptedEmbeddings(during=_hang)).ingest_next())
    await inside.wait()
    killed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await killed
    assert await world.status(source.id) == (SourceStatus.INGESTING, None)

    world.clock.advance(_LEASE + timedelta(seconds=1))
    reports = await world.ingest_once()

    assert reports == [IngestReport(source_id=source.id, result="ingested", chunks=3)]
    ordinals = [chunk.ordinal for chunk in await world.chunks(source.id)]
    assert ordinals == [0, 1, 2]


async def test_a_live_lease_is_not_claimed_twice(world: _World) -> None:
    source = await world.add("ac2-live")
    claimed = await world.queue.claim_next(
        claim_id=UUID(int=1), now=world.clock.now(), lease_until=world.clock.now() + _LEASE
    )
    assert claimed is not None
    assert claimed.id == source.id

    world.clock.advance(_LEASE - timedelta(seconds=1))
    reports = await world.ingest_once()

    assert source.id not in [report.source_id for report in reports]
    assert await world.status(source.id) == (SourceStatus.INGESTING, None)


async def test_an_ingesting_source_with_no_lease_is_taken_over(world: _World) -> None:
    source = await world.add("no-lease", status=SourceStatus.INGESTING)

    reports = await world.ingest_once()

    assert reports == [IngestReport(source_id=source.id, result="ingested", chunks=3)]


async def test_a_failed_reingest_keeps_the_last_chunks_and_their_count(world: _World) -> None:
    source = await world.add("failed-reingest")
    await world.ingest_once()
    assert isinstance(await world.service.reingest(world.admin.id, source.id), ReingestQueued)

    await world.ingest_once(ScriptedEmbeddings(EmbeddingFailed(reason=EmbeddingFailure.ERROR)))

    listed = next(s for s in await world.sources.list_all() if s.id == source.id)
    assert (listed.status, listed.chunk_count, listed.embedding_model) == (
        SourceStatus.FAILED,
        3,
        MODEL,
    )
    assert len(await world.chunks(source.id)) == 3


async def test_a_heartbeat_extends_only_its_own_live_claim(world: _World) -> None:
    source = await world.add("heartbeat")
    now = world.clock.now()
    mine, theirs = UUID(int=7), UUID(int=8)
    await world.queue.claim_next(claim_id=mine, now=now, lease_until=now + _LEASE)
    later = now + 2 * _LEASE

    assert await world.queue.heartbeat(source.id, claim_id=theirs, lease_until=later) is False
    assert await world.queue.heartbeat(source.id, claim_id=mine, lease_until=later) is True

    world.clock.advance(_LEASE + timedelta(seconds=1))
    # The renewed lease still holds past the first one's end, so nothing is claimed.
    assert source.id not in [r.source_id for r in await world.ingest_once()]
    await world.service.delete(source.id)
    assert await world.queue.heartbeat(source.id, claim_id=mine, lease_until=later) is False


async def test_a_reingest_replaces_the_chunks_in_one_go(world: _World) -> None:
    source = await world.add("reingest-replace")
    await world.ingest_once()
    first = {chunk.id for chunk in await world.chunks(source.id)}

    assert isinstance(await world.service.reingest(world.admin.id, source.id), ReingestQueued)
    await world.ingest_once()

    second = await world.chunks(source.id)
    assert [chunk.ordinal for chunk in second] == [0, 1, 2]
    assert first.isdisjoint({chunk.id for chunk in second})


async def test_an_embedding_failure_fails_the_source_and_reingest_recovers_it(
    world: _World,
) -> None:
    source = await world.add("ac3")
    failing = ScriptedEmbeddings(EmbeddingFailed(reason=EmbeddingFailure.ERROR))

    await world.ingest_once(failing)

    assert await world.status(source.id) == (SourceStatus.FAILED, "embedding_failed")
    assert await world.chunks(source.id) == []

    assert isinstance(await world.service.reingest(world.admin.id, source.id), ReingestQueued)
    reports = await world.ingest_once()

    assert reports == [IngestReport(source_id=source.id, result="ingested", chunks=3)]
    assert await world.status(source.id) == (SourceStatus.INGESTED, None)


async def test_a_type_with_no_reader_fails_with_a_reason(world: _World) -> None:
    source = await world.add("ac4-epub", data=b"PK\x03\x04", file_type=KnowledgeFileType.EPUB)

    reports = await world.ingest_once()

    assert [(r.result, r.reason) for r in reports if r.source_id == source.id] == [
        ("failed", "no_reader")
    ]
    assert await world.status(source.id) == (SourceStatus.FAILED, "no_reader")


_PDF_PAGES = [
    ["Deload weeks restore strength after a hard block of training."],
    ["Sleep and food matter more than any single session of the week."],
]


async def test_an_uploaded_pdf_reaches_ingested_and_its_text_is_searchable(
    world: _World,
) -> None:
    source = await world.add(
        "pdf-found", data=text_pdf(_PDF_PAGES), file_type=KnowledgeFileType.PDF
    )
    source_id = source.id

    reports = await world.ingest_once()

    assert [(r.source_id, r.result, r.chunks) for r in reports] == [(source_id, "ingested", 2)]
    assert await world.status(source_id) == (SourceStatus.INGESTED, None)
    async with world.factory() as session:
        hit = await session.scalar(
            text(
                "SELECT count(*) FROM knowledge_chunks WHERE source_id = :id "
                "AND search_vector @@ to_tsquery('english', 'deload & strength')"
            ),
            {"id": source_id},
        )
    assert hit == 1


async def test_each_pdf_chunk_records_the_page_it_came_from(world: _World) -> None:
    source = await world.add(
        "pdf-pages",
        data=text_pdf(
            [*_PDF_PAGES, ["Printed numbering restarts here."]],
            labels="0 << /S /r >> 2 << /S /D >>",
        ),
        file_type=KnowledgeFileType.PDF,
    )

    await world.ingest_once()

    assert [c.locator for c in await world.chunks(source.id)] == [
        {"kind": "page", "page": 1, "page_label": "i"},
        {"kind": "page", "page": 2, "page_label": "ii"},
        {"kind": "page", "page": 3, "page_label": "1"},
    ]


async def test_a_scanned_pdf_fails_with_a_reason_that_names_the_problem(world: _World) -> None:
    source = await world.add("pdf-scan", data=blank_pdf(), file_type=KnowledgeFileType.PDF)

    await world.ingest_once()

    assert await world.status(source.id) == (SourceStatus.FAILED, "no_text_layer")
    assert await world.chunks(source.id) == []


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"%PDF-1.7 broken", "corrupt_file"),
        (encrypted_pdf(text_pdf(_PDF_PAGES), user_password="pw"), "encrypted_file"),
    ],
    ids=["corrupt", "encrypted"],
)
async def test_a_bad_pdf_fails_and_the_worker_ingests_the_next_source(
    world: _World, data: bytes, reason: str
) -> None:
    bad = await world.add(f"pdf-bad-{reason}", data=data, file_type=KnowledgeFileType.PDF)
    good = await world.add(f"pdf-next-{reason}", minutes=1)

    first = await world.ingest_once()
    second = await world.ingest_once()

    assert [(r.source_id, r.result, r.reason) for r in first + second] == [
        (bad.id, "failed", reason),
        (good.id, "ingested", None),
    ]


@pytest.mark.parametrize(
    "status",
    [SourceStatus.PROPOSED, SourceStatus.DETECTING, SourceStatus.REJECTED],
    ids=["proposed", "detecting", "rejected"],
)
async def test_proposed_detecting_and_rejected_sources_are_never_read(
    world: _World, status: SourceStatus
) -> None:
    waiting = await world.add(f"ac4-{status}", status=status)
    embeddings = ScriptedEmbeddings()

    reports = await world.ingest_once(embeddings)

    assert waiting.id not in [report.source_id for report in reports]
    assert waiting.storage_key not in world.storage.reads
    assert embeddings.calls == []
    assert await world.status(waiting.id) == (status, None)


async def test_a_source_deleted_mid_ingest_leaves_no_chunks(world: _World) -> None:
    source = await world.add("ac5")

    async def _delete() -> None:
        await world.service.delete(source.id)

    reports = await world.ingest_once(ScriptedEmbeddings(during=_delete))

    assert reports == [IngestReport(source_id=source.id, result="gone")]
    assert await world.sources.get(source.id) is None
    assert await world.chunks(source.id) == []


async def test_deleting_an_ingested_source_removes_its_chunks(world: _World) -> None:
    source = await world.add("ac5-after")
    await world.ingest_once()

    await world.service.delete(source.id)

    assert await world.chunks(source.id) == []


async def test_each_chunk_records_its_model_heading_path_and_locator(world: _World) -> None:
    source = await world.add("ac6")

    await world.ingest_once()

    chunks = await world.chunks(source.id)
    assert [(c.heading_path, c.locator) for c in chunks] == [
        (["Deload guide", "Why"], {"kind": "section", "section": "Why"}),
        (["Deload guide", "Timing"], {"kind": "section", "section": "Timing"}),
        (["Deload guide", "Volume"], {"kind": "section", "section": "Volume"}),
    ]
    assert {c.embedding_model for c in chunks} == {MODEL}
    assert {c.section_kind for c in chunks} == {SectionKind.BODY.value}
    assert all(len(c.embedding) == DIMENSIONS for c in chunks)
    assert {(tuple(c.sports), tuple(c.topics), c.language) for c in chunks} == {
        (("gym",), ("deload",), "en")
    }
    assert all(c.exercise_id is None for c in chunks)
    assert not any("Smith" in c.text for c in chunks)


@pytest.mark.parametrize(
    ("language", "query", "matches"),
    [
        (SourceLanguage.EN, "english:guide:A & deload:B & fatigue:D", True),
        (SourceLanguage.EN, "english:fatigue:A", False),
        (SourceLanguage.PL, "simple:guide:A & deload:B & fatigue:D", True),
    ],
    ids=["english_weights", "english_body_not_a", "polish_simple"],
)
async def test_the_tsvector_weights_title_a_headings_b_and_text_d(
    world: _World, language: SourceLanguage, query: str, matches: bool
) -> None:
    config, terms = query.split(":", 1)
    source = await world.add(f"fts-{language}-{matches}", language=language)
    await world.ingest_once()

    async with world.factory() as session:
        found = await session.scalar(
            text(
                "SELECT bool_or(search_vector @@ to_tsquery(CAST(:config AS regconfig), :terms))"
                " FROM knowledge_chunks WHERE source_id = :source AND ordinal = 0"
            ),
            {"config": config, "terms": terms, "source": source.id},
        )
    assert found is matches


async def test_knowledge_chunks_hold_no_user_reference(world: _World) -> None:
    async with world.factory() as session:
        referenced = await session.scalars(
            text(
                "SELECT DISTINCT confrelid::regclass::text FROM pg_constraint"
                " WHERE conrelid = 'knowledge_chunks'::regclass AND contype = 'f'"
            )
        )
        columns = await session.scalars(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'knowledge_chunks'"
            )
        )
        assert set(referenced) == {"knowledge_sources", "exercises"}
        assert not [name for name in columns if "user" in name]
