"""The exercise catalogue's texts as searchable knowledge on real PostgreSQL (ADR-0033 *Catalogue
texts*, ADR-0036's #427 amendment, PRD B7, G5): one test per acceptance criterion, with the
embedding gateway faked at its port and `search_knowledge` called through `Client(server)`.

The catalogue's sources are one row per language, shared by every test, so this module runs in
the `db_schema` group (tests/conftest.py). Queueing dates the sources 2001, so a claim takes them
before any committed source. Like `test_first_gym_session_replay.py`, it expects the worktree's
database to hold no imported catalogue: a real one's chunks would crowd the sample's out of a
search, and its sources out of the counts.
"""

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastmcp import Client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.catalogue_knowledge_repository import (
    SqlAlchemyCatalogueChunks,
    SqlAlchemyCatalogueSources,
)
from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.knowledge_chunk_repository import SqlAlchemyKnowledgeIngestionRepository
from ai_trainer.adapters.knowledge_repository import SqlAlchemyKnowledgeSourcesRepository
from ai_trainer.adapters.knowledge_search_repository import SqlAlchemyKnowledgeSearch
from ai_trainer.adapters.orm import ExerciseOrm, KnowledgeChunkOrm
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.exercise_catalogue import CatalogueQueueing, import_catalogue
from ai_trainer.application.knowledge_ingestion import KnowledgeIngestion
from ai_trainer.application.knowledge_search import KnowledgeSearch
from ai_trainer.application.training_history import TrainingHistory
from ai_trainer.domain.catalogue_knowledge import catalogue_content_hash
from ai_trainer.domain.embeddings import (
    Embedded,
    EmbeddingFailed,
    EmbeddingFailure,
    EmbeddingOutcome,
    QueryTask,
)
from ai_trainer.domain.knowledge import (
    EvidenceTier,
    KnowledgeSource,
    LicenseId,
    SourceLanguage,
    SourceStatus,
    SourceType,
)
from ai_trainer.domain.knowledge_document import SectionLocator
from ai_trainer.domain.knowledge_ingestion import IngestFailure, IngestReport
from ai_trainer.domain.knowledge_search import KnowledgeSearchPage
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.mcp.identity import USER_ID_META_KEY
from ai_trainer.mcp.results import ToolResult
from ai_trainer.mcp.server import SEARCH_KNOWLEDGE_TOOL, build_mcp_server
from tests.exercise_support import SAMPLE, dataset_transport, sample_source
from tests.ingestion_support import DIMENSIONS, MODEL, FrozenClock, ingestion
from tests.knowledge_support import MemoryStorage
from tests.mcp_support import no_exercise_tools

type Factory = Callable[[], AsyncSession]

_LONG_AGO = datetime(2001, 1, 1, tzinfo=UTC)
_ADMIN_EMAIL = "catalogue-admin@example.com"
_ATHLETE = UUID("00000000-0000-4000-8000-000000000427")
_PULL_APART = "zz-test-03"


def _direction(name: str) -> tuple[float, ...]:
    """A unit vector per exercise name, so a passage is nearest the question about it."""
    vector = [0.0] * DIMENSIONS
    vector[sum(name.encode()) % DIMENSIONS] = 1.0
    return tuple(vector)


def _name(embedded_input: str) -> str:
    # `Exercise catalogue — <name>`, then the text (ADR-0036's embedding input).
    return embedded_input.split("\n", 1)[0].split(" — ", 1)[1]


class _ByName:
    """Implements `EmbeddingGatewayPort`: each passage points the way of its exercise's name.
    `fail_call` makes that call (1-based) fail, as an upstream error would."""

    def __init__(self, *, fail_call: int | None = None) -> None:
        self.fail_call = fail_call
        self.calls: list[list[str]] = []

    @property
    def embedded(self) -> list[str]:
        return [text for call in self.calls for text in call]

    async def embed_documents(
        self, texts: Sequence[str], *, acting_user_id: UUID
    ) -> EmbeddingOutcome:
        self.calls.append(list(texts))
        if len(self.calls) == self.fail_call:
            return EmbeddingFailed(reason=EmbeddingFailure.ERROR)
        return Embedded(vectors=tuple(_direction(_name(text)) for text in texts), model=MODEL)


class _Question:
    """Implements `QueryEmbeddingPort`: the question points the way of one exercise's name."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def embed_query(
        self, text: str, *, acting_user_id: UUID, task: QueryTask = QueryTask.QUESTION
    ) -> EmbeddingOutcome:
        return Embedded(vectors=(_direction(self.name),), model=MODEL)


class _NoSessions:
    async def list_sessions(self, *args: Any, **kwargs: Any) -> Sequence[Any]:
        return []


class _World:
    def __init__(self, factory: Factory, admin: User) -> None:
        self.factory = factory
        self.admin = admin
        self.clock = FrozenClock(_LONG_AGO)
        self.sources = SqlAlchemyKnowledgeSourcesRepository(factory)
        self.chunk_repository = SqlAlchemyCatalogueChunks(factory)

    async def import_sample(self, body: bytes | None = None) -> int:
        report = await import_catalogue(
            source=sample_source(dataset_transport(body or SAMPLE.read_bytes())),
            exercises=SqlAlchemyExerciseRepository(self.factory),
            knowledge=CatalogueQueueing(
                sources=SqlAlchemyCatalogueSources(self.factory),
                admin_emails=[_ADMIN_EMAIL.upper()],
                clock=self.clock,
            ),
        )
        return report.catalogue_queued

    def worker(self, embeddings: _ByName, *, batch_size: int = 32) -> KnowledgeIngestion:
        return ingestion(
            repository=SqlAlchemyKnowledgeIngestionRepository(self.factory),
            storage=MemoryStorage(),
            embeddings=embeddings,
            clock=self.clock,
            batch_size=batch_size,
            catalogue=self.chunk_repository,
        )

    async def ingest(
        self, gateways: dict[SourceLanguage, _ByName], *, batch_size: int = 32
    ) -> dict[SourceLanguage, IngestReport]:
        """One claim per waiting catalogue source named, each with its own gateway. A claim
        takes the oldest waiting source, and both are queued at one time, so by ID."""
        waiting = sorted(
            [
                source
                for language in gateways
                if (source := await self.source(language)).status is SourceStatus.UPLOADED
            ],
            key=lambda source: source.id,
        )
        reports: dict[SourceLanguage, IngestReport] = {}
        for source in waiting:
            language = SourceLanguage(source.language)
            worker = self.worker(gateways[language], batch_size=batch_size)
            report = await worker.ingest_next()
            assert report is not None
            assert report.source_id == source.id
            reports[language] = report
        return reports

    async def ingest_catalogue(self, embeddings: _ByName) -> dict[SourceLanguage, IngestReport]:
        return await self.ingest(dict.fromkeys(SourceLanguage, embeddings))

    async def source(self, language: SourceLanguage) -> KnowledgeSource:
        source = await self.sources.get_by_hash(catalogue_content_hash(language))
        assert source is not None
        return source

    async def exercise_id(self, ref: str) -> UUID:
        async with self.factory() as session:
            found = await session.scalar(
                select(ExerciseOrm.id).where(ExerciseOrm.source_ref == ref)
            )
        assert found is not None
        return found

    async def chunks(self, language: SourceLanguage) -> list[KnowledgeChunkOrm]:
        source = await self.source(language)
        async with self.factory() as session:
            rows = await session.scalars(
                select(KnowledgeChunkOrm)
                .where(KnowledgeChunkOrm.source_id == source.id)
                .order_by(KnowledgeChunkOrm.ordinal)
            )
            return list(rows)

    async def text_count(self, language: SourceLanguage) -> int:
        return len(await self.chunk_repository.texts(language))

    async def search(self, query: str, *, near: str, **args: Any) -> KnowledgeSearchPage:
        registry = default_sport_registry()
        server = build_mcp_server(
            TrainingHistory(_NoSessions(), registry),
            *no_exercise_tools(),
            knowledge=KnowledgeSearch(
                embeddings=_Question(near),
                repository=SqlAlchemyKnowledgeSearch(self.factory),
                sports=registry,
                embedding_model=MODEL,
                depth=20,
            ),
        )
        async with Client(server) as client:
            result = await client.call_tool(
                SEARCH_KNOWLEDGE_TOOL,
                {"query": query, **args},
                meta={USER_ID_META_KEY: str(_ATHLETE)},
            )
        parsed = ToolResult[KnowledgeSearchPage].model_validate(result.structured_content)
        assert parsed.data is not None, parsed.error
        return parsed.data


@pytest.fixture
async def world(db_session_factory: Factory) -> _World:
    admin = await SqlAlchemyUsersRepository(db_session_factory).create(
        NewUser(
            sub="catalogue-admin",
            email=_ADMIN_EMAIL,
            name="Admin",
            locale="en",
            status=UserStatus.ACTIVE,
        )
    )
    return _World(db_session_factory, admin)


async def test_an_english_question_finds_a_polish_catalogue_passage(world: _World) -> None:
    await world.import_sample()
    await world.ingest_catalogue(_ByName())
    pull_apart = await world.exercise_id(_PULL_APART)

    page = await world.search(
        "how do I stretch a resistance band out to my sides", near="Band Pull Apart", sport="gym"
    )

    polish = [
        hit
        for hit in page.hits
        if hit.exercise_id == pull_apart and hit.language is SourceLanguage.PL
    ]
    assert len(polish) == 1
    assert polish[0].text == "Rozciągnij gumę.\n\nTarget: delts. Equipment: band."
    assert (polish[0].title, polish[0].evidence_tier, polish[0].source_type) == (
        "Exercise catalogue",
        EvidenceTier.CATALOGUE,
        SourceType.CATALOGUE,
    )
    assert polish[0].locator == SectionLocator(section="Band Pull Apart")


async def test_search_by_exercise_id_returns_only_that_exercises_chunks(world: _World) -> None:
    await world.import_sample()
    await world.ingest_catalogue(_ByName())
    pull_apart = await world.exercise_id(_PULL_APART)

    page = await world.search("chest press", near="Lever Chest Press", exercise_id=str(pull_apart))

    assert {hit.exercise_id for hit in page.hits} == {pull_apart}
    assert {hit.language for hit in page.hits} == {SourceLanguage.EN, SourceLanguage.PL}


async def test_a_second_run_with_no_changed_text_embeds_nothing(world: _World) -> None:
    assert await world.import_sample() == 2
    await world.ingest_catalogue(_ByName())
    assert await world.import_sample() == 0

    # Queued anyway, as a re-ingest from the admin page would: still nothing to embed.
    for language in SourceLanguage:
        await world.sources.queue_reingest(
            (await world.source(language)).id, requested_by=world.admin.id
        )
    again = _ByName()
    reports = await world.ingest_catalogue(again)

    assert again.calls == []
    assert {report.result for report in reports.values()} == {"ingested"}
    for language in SourceLanguage:
        source = await world.source(language)
        assert source.chunk_count == await world.text_count(language)


async def test_a_changed_text_replaces_its_own_chunk_and_no_other(world: _World) -> None:
    await world.import_sample()
    await world.ingest_catalogue(_ByName())
    before = {chunk.exercise_id: chunk for chunk in await world.chunks(SourceLanguage.PL)}
    pull_apart = await world.exercise_id(_PULL_APART)
    english_before = {chunk.id for chunk in await world.chunks(SourceLanguage.EN)}

    sample = json.loads(SAMPLE.read_text())
    for row in sample:
        if row["id"] == _PULL_APART:
            row["instructions"]["pl"] = "Rozciągnij gumę na boki."
    # Only the Polish source has a stale text, so only it is queued.
    assert await world.import_sample(json.dumps(sample).encode()) == 1
    changed = _ByName()
    await world.ingest_catalogue(changed)

    new_text = "Rozciągnij gumę na boki.\n\nTarget: delts. Equipment: band."
    assert changed.embedded == [f"Exercise catalogue — Band Pull Apart\n\n{new_text}"]
    after = {chunk.exercise_id: chunk for chunk in await world.chunks(SourceLanguage.PL)}
    assert after.keys() == before.keys()
    assert after[pull_apart].text == new_text
    assert after[pull_apart].ordinal == before[pull_apart].ordinal
    for exercise_id, chunk in after.items():
        if exercise_id != pull_apart:
            assert (chunk.id, chunk.created_at) == (
                before[exercise_id].id,
                before[exercise_id].created_at,
            )
    assert {chunk.id for chunk in await world.chunks(SourceLanguage.EN)} == english_before


async def test_a_changed_target_requeues_and_reembeds_only_its_exercise(world: _World) -> None:
    # #508: the chunk carries the exercise's target and equipment, so a change to either is a
    # changed text in both languages, and an import with nothing changed queues nothing.
    await world.import_sample()
    await world.ingest_catalogue(_ByName())
    assert await world.import_sample() == 0

    sample = json.loads(SAMPLE.read_text())
    for row in sample:
        if row["id"] == _PULL_APART:
            row["target"] = "rear delts"
    assert await world.import_sample(json.dumps(sample).encode()) == 2
    changed = _ByName()
    await world.ingest_catalogue(changed)

    assert sorted(changed.embedded) == [
        "Exercise catalogue — Band Pull Apart\n\n"
        "Pull the band apart.\n\nTarget: rear delts. Equipment: band.",
        "Exercise catalogue — Band Pull Apart\n\n"
        "Rozciągnij gumę.\n\nTarget: rear delts. Equipment: band.",
    ]
    assert await world.import_sample(json.dumps(sample).encode()) == 0


async def test_a_failed_batch_keeps_earlier_chunks_and_a_rerun_finishes(world: _World) -> None:
    await world.import_sample()
    english = await world.source(SourceLanguage.EN)

    reports = await world.ingest(
        {SourceLanguage.EN: _ByName(fail_call=2), SourceLanguage.PL: _ByName()}, batch_size=2
    )

    assert reports[SourceLanguage.EN] == IngestReport(
        source_id=english.id, result="failed", reason=IngestFailure.EMBEDDING_FAILED
    )
    kept = await world.chunks(SourceLanguage.EN)
    assert len(kept) == 2
    assert (await world.source(SourceLanguage.EN)).chunk_count == 2

    await world.sources.queue_reingest(english.id, requested_by=world.admin.id)
    rerun = _ByName()
    await world.ingest({SourceLanguage.EN: rerun}, batch_size=2)

    total = await world.text_count(SourceLanguage.EN)
    assert len(rerun.embedded) == total - 2
    finished = await world.source(SourceLanguage.EN)
    assert (finished.status, finished.chunk_count) == (SourceStatus.INGESTED, total)
    assert {chunk.id for chunk in kept} <= {
        chunk.id for chunk in await world.chunks(SourceLanguage.EN)
    }


async def test_the_sources_are_licensed_and_run_as_the_oldest_listed_admin(world: _World) -> None:
    await world.import_sample()

    for language in SourceLanguage:
        source = await world.source(language)
        assert (source.status, source.uploaded_by_user_id) == (
            SourceStatus.UPLOADED,
            world.admin.id,
        )
        assert (source.license_id, source.sports, source.file_type) == (
            LicenseId.OTHER,
            ("gym",),
            None,
        )
        # The dataset's, which most rows carry, never the in-house `extra.yaml` row's.
        assert source.attribution is not None
        assert "(MIT)" in source.attribution


async def test_english_chunks_use_english_full_text_and_polish_ones_simple(world: _World) -> None:
    await world.import_sample()
    await world.ingest_catalogue(_ByName())
    pull_apart = await world.exercise_id(_PULL_APART)

    english = next(c for c in await world.chunks(SourceLanguage.EN) if c.exercise_id == pull_apart)
    polish = next(c for c in await world.chunks(SourceLanguage.PL) if c.exercise_id == pull_apart)

    # `english` stems and drops stop words; `simple` only lowercases (ADR-0033). The name is A.
    # The text is weighted D, and since #508 it ends with the target and equipment line.
    assert str(english.search_vector) == (
        "'apart':3A,7 'band':1A,6,11 'delt':9 'equip':10 'pull':2A,4 'target':8"
    )
    assert str(polish.search_vector) == (
        "'apart':3A 'band':1A,9 'delts':7 'equipment':8 'gumę':5 'pull':2A 'rozciągnij':4"
        " 'target':6"
    )


async def test_a_requeue_mid_ingest_leaves_the_running_worker_writing_nothing(
    world: _World,
) -> None:
    await world.import_sample()
    english = await world.source(SourceLanguage.EN)
    queue = SqlAlchemyKnowledgeIngestionRepository(world.factory)
    old_claim = UUID(int=427)
    now = world.clock.now()
    claimed: set[UUID] = set()
    while english.id not in claimed:
        taken = await queue.claim_next(claim_id=old_claim, now=now, lease_until=now)
        assert taken is not None
        claimed.add(taken.id)

    sample = json.loads(SAMPLE.read_text())
    sample[0]["instructions"]["en"] = "Press the bar up from your chest."
    # Every claimed source is stale (an `uploaded` one would be waiting already), so each goes
    # back, its claim with it.
    assert await world.import_sample(json.dumps(sample).encode()) == len(claimed)

    assert (await world.source(SourceLanguage.EN)).status is SourceStatus.UPLOADED
    assert not await world.chunk_repository.write_batch(
        english.id, claim_id=old_claim, chunks=[], model=MODEL, at=now
    )
    assert not await world.chunk_repository.finish(
        english.id, claim_id=old_claim, keep=frozenset(), model=MODEL, ingested_at=now
    )


async def test_with_no_listed_admin_the_sources_wait_and_fail_without_an_acting_user(
    world: _World,
) -> None:
    await import_catalogue(
        source=sample_source(),
        exercises=SqlAlchemyExerciseRepository(world.factory),
        knowledge=CatalogueQueueing(
            sources=SqlAlchemyCatalogueSources(world.factory),
            admin_emails=["someone-else@example.com"],
            clock=world.clock,
        ),
    )
    nobody = _ByName()

    reports = await world.ingest_catalogue(nobody)

    assert {report.reason for report in reports.values()} == {IngestFailure.NO_ACTING_ADMIN}
    assert nobody.calls == []


async def test_with_admin_emails_unset_no_one_is_the_acting_user(world: _World) -> None:
    queued = await SqlAlchemyCatalogueSources(world.factory).queue(
        attribution=None, admin_emails=[], now=world.clock.now()
    )

    assert queued == 2
    for language in SourceLanguage:
        assert (await world.source(language)).uploaded_by_user_id is None


async def test_an_import_after_one_that_never_queued_still_queues_the_stale_texts(
    world: _World,
) -> None:
    await world.import_sample()
    await world.ingest_catalogue(_ByName())
    sample = json.loads(SAMPLE.read_text())
    sample[1]["instructions"]["en"] = "Jump your feet apart and back together."
    # An import whose rows committed but which never reached the queueing step.
    await import_catalogue(
        source=sample_source(dataset_transport(json.dumps(sample).encode())),
        exercises=SqlAlchemyExerciseRepository(world.factory),
    )

    assert await world.import_sample(json.dumps(sample).encode()) == 1
    fixed = _ByName()
    await world.ingest({SourceLanguage.EN: fixed})
    assert len(fixed.embedded) == 1
    assert "\n\nJump your feet apart and back together.\n\nTarget: " in fixed.embedded[0]


async def test_a_failed_source_is_queued_again_by_a_plain_rerun(world: _World) -> None:
    await world.import_sample()
    await world.ingest(
        {SourceLanguage.EN: _ByName(fail_call=1), SourceLanguage.PL: _ByName()}, batch_size=64
    )
    assert (await world.source(SourceLanguage.EN)).status is SourceStatus.FAILED

    assert await world.import_sample() == 1
    assert (await world.source(SourceLanguage.EN)).status is SourceStatus.UPLOADED
