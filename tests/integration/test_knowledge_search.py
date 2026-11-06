"""`search_knowledge`'s two legs on real PostgreSQL and pgvector (ADR-0033 *Retrieval*, #424),
through the use case with the query embedding faked at its port.

Each test writes its chunks under its own embedding-model name, so the search, which always
filters on the model, never sees another test's chunks or the worktree's dev data. With
`depth = 1` each leg contributes exactly its best passage, which shows which leg found what.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.knowledge_chunk_repository import search_vector
from ai_trainer.adapters.knowledge_repository import SqlAlchemyKnowledgeSourcesRepository
from ai_trainer.adapters.knowledge_search_repository import SqlAlchemyKnowledgeSearch
from ai_trainer.adapters.orm import KnowledgeChunkOrm
from ai_trainer.application.knowledge_search import KnowledgeSearch
from ai_trainer.domain.embeddings import Embedded, EmbeddingOutcome
from ai_trainer.domain.knowledge import (
    EvidenceTier,
    KnowledgeSource,
    SourceLanguage,
    SourceStatus,
)
from ai_trainer.domain.knowledge_document import (
    CueLocator,
    Locator,
    PageLocator,
    SectionLocator,
    SpineLocator,
)
from ai_trainer.domain.knowledge_search import (
    KnowledgeSearchFailed,
    KnowledgeSearchPage,
    KnowledgeSearchQuery,
)
from ai_trainer.domain.sports.registry import default_sport_registry
from tests.ingestion_support import DIMENSIONS, uploaded_source

type Factory = Callable[[], AsyncSession]

_ATHLETE = UUID("00000000-0000-4000-8000-000000000424")
_AT = datetime(2026, 11, 4, tzinfo=UTC)


def _axis(index: int, *, lean: int | None = None) -> list[float]:
    """A unit vector on `index`, tilted a little towards `lean`: near its axis, far from the
    others by cosine distance."""
    vector = [0.0] * DIMENSIONS
    vector[index] = 1.0
    if lean is not None:
        vector[lean] = 0.1
    return vector


class _QueryVector:
    """Implements `QueryEmbeddingPort`: every question embeds to axis 0."""

    def __init__(self, model: str) -> None:
        self.model = model

    async def embed_query(self, text: str, *, acting_user_id: UUID) -> EmbeddingOutcome:
        return Embedded(vectors=(tuple(_axis(0)),), model=self.model)


class _World:
    def __init__(self, factory: Factory, source: KnowledgeSource, polish: KnowledgeSource) -> None:
        self.factory = factory
        self.source = source
        self.polish = polish
        self.model = f"test-model-{uuid4()}"
        self._ordinal = 0

    async def chunk(
        self,
        body: str,
        vector: list[float],
        *,
        model: str | None = None,
        sports: Sequence[str] = ("gym",),
        topics: Sequence[str] = ("deload",),
        polish: bool = False,
        locator: Locator | None = None,
    ) -> UUID:
        source = self.polish if polish else self.source
        chunk_id = uuid4()
        self._ordinal += 1
        async with self.factory() as session:
            await session.execute(
                insert(KnowledgeChunkOrm).values(
                    id=chunk_id,
                    source_id=source.id,
                    ordinal=self._ordinal,
                    text=body,
                    embedding=vector,
                    embedding_model=model or self.model,
                    sports=list(sports),
                    topics=list(topics),
                    language=source.language.value,
                    heading_path=["Recovery"],
                    section_kind="body",
                    locator=(locator or SectionLocator(section="Recovery")).model_dump(mode="json"),
                    search_vector=search_vector(
                        language=source.language,
                        title=source.title,
                        headings=["Recovery"],
                        body=body,
                    ),
                    created_at=_AT,
                )
            )
            await session.commit()
        return chunk_id

    async def search(
        self, query: KnowledgeSearchQuery, *, depth: int = 1
    ) -> KnowledgeSearchPage | KnowledgeSearchFailed:
        use_case = KnowledgeSearch(
            embeddings=_QueryVector(self.model),
            repository=SqlAlchemyKnowledgeSearch(self.factory),
            sports=default_sport_registry(),
            embedding_model=self.model,
            depth=depth,
        )
        return await use_case.search(_ATHLETE, query)

    async def found(self, query: KnowledgeSearchQuery, *, depth: int = 1) -> list[UUID]:
        page = await self.search(query, depth=depth)
        assert isinstance(page, KnowledgeSearchPage), page
        return [hit.chunk_id for hit in page.hits]


@pytest.fixture
async def world(db_session_factory: Factory) -> _World:
    sources = SqlAlchemyKnowledgeSourcesRepository(db_session_factory)
    english = await sources.add(
        uploaded_source(admin_id=None, marker=f"en-{uuid4()}", status=SourceStatus.INGESTED)
    )
    polish = await sources.add(
        uploaded_source(
            admin_id=None,
            marker=f"pl-{uuid4()}",
            status=SourceStatus.INGESTED,
            language=SourceLanguage.PL,
        )
    )
    return _World(db_session_factory, english, polish)


async def test_a_passage_matching_only_by_meaning_is_found(world: _World) -> None:
    meaning = await world.chunk("Lighter weeks let accumulated tiredness fade.", _axis(0, lean=1))
    words = await world.chunk("Supercompensation follows overload.", _axis(5))

    found = await world.found(KnowledgeSearchQuery(text="when should I take a deload week"))

    assert meaning in found
    assert words not in found


async def test_a_passage_matching_only_by_exact_words_is_found(world: _World) -> None:
    meaning = await world.chunk("Lighter weeks let accumulated tiredness fade.", _axis(0, lean=1))
    words = await world.chunk("Supercompensation follows planned overload.", _axis(5))

    found = await world.found(KnowledgeSearchQuery(text="supercompensation"))

    assert words in found
    assert meaning in found  # the vector leg's best, fused with the full-text leg's
    assert set(found) == {meaning, words}


async def test_a_passage_found_by_both_legs_comes_first(world: _World) -> None:
    await world.chunk("Lighter weeks let accumulated tiredness fade.", _axis(0, lean=1))
    both = await world.chunk("Supercompensation after lighter weeks.", _axis(0, lean=2))
    await world.chunk("Supercompensation follows planned overload.", _axis(5))

    found = await world.found(KnowledgeSearchQuery(text="supercompensation"), depth=2)

    assert found[0] == both


async def test_a_polish_chunk_is_found_by_meaning_for_an_english_query(world: _World) -> None:
    polish = await world.chunk(
        "Lżejszy tydzień pozwala zniknąć zmęczeniu.", _axis(0, lean=1), polish=True
    )
    await world.chunk("Supercompensation follows planned overload.", _axis(5))

    found = await world.found(KnowledgeSearchQuery(text="when should I take an easier week"))

    assert polish in found


async def test_a_polish_chunk_is_found_by_its_exact_words(world: _World) -> None:
    polish = await world.chunk("Roztrenowanie po sezonie.", _axis(7), polish=True)
    await world.chunk("Lighter weeks let accumulated tiredness fade.", _axis(0, lean=1))

    found = await world.found(KnowledgeSearchQuery(text="roztrenowanie"))

    assert polish in found


async def test_a_sport_filter_leaves_other_sports_chunks_out(world: _World) -> None:
    gym = await world.chunk("Deload the bench press.", _axis(0, lean=1), sports=("gym",))
    climbing = await world.chunk("Deload the fingerboard.", _axis(0, lean=2), sports=("climbing",))
    both = await world.chunk("Deload everything.", _axis(0, lean=3), sports=("gym", "climbing"))

    found = await world.found(KnowledgeSearchQuery(text="deload", sport_id="climbing"), depth=20)

    assert set(found) == {climbing, both}
    assert gym not in found


async def test_chunks_from_another_embedding_model_are_never_returned(world: _World) -> None:
    ours = await world.chunk("Deload every fourth week.", _axis(0, lean=1))
    # Nearer the query and holding its words: both legs would rank it first.
    theirs = await world.chunk("Deload deload every week.", _axis(0), model="other/embedder")

    found = await world.found(KnowledgeSearchQuery(text="deload"), depth=20)

    assert found == [ours]
    assert theirs not in found


async def test_a_topic_filter_keeps_chunks_with_any_of_the_topics(world: _World) -> None:
    sleep = await world.chunk("Sleep before a deload.", _axis(0, lean=1), topics=("sleep",))
    volume = await world.chunk("Deload volume.", _axis(0, lean=2), topics=("volume", "deload"))
    await world.chunk("Deload nutrition.", _axis(0, lean=3), topics=("nutrition",))

    found = await world.found(
        KnowledgeSearchQuery(text="deload", topics=("Sleep", "volume")), depth=20
    )

    assert set(found) == {sleep, volume}


async def test_an_exercise_filter_keeps_only_that_exercises_chunks(world: _World) -> None:
    await world.chunk("Deload the bench press.", _axis(0, lean=1))

    outcome = await world.search(KnowledgeSearchQuery(text="deload", exercise_id=uuid4()))

    assert outcome == KnowledgeSearchFailed(reason="no_match")


async def test_no_match_is_a_failure(world: _World) -> None:
    outcome = await world.search(KnowledgeSearchQuery(text="deload"))
    assert outcome == KnowledgeSearchFailed(reason="no_match")


@pytest.mark.parametrize(
    "locator",
    [
        SectionLocator(section="Recovery"),
        SectionLocator(),
        PageLocator(page=12, page_label="xii"),
        SpineLocator(spine_item="chapter-3.xhtml"),
        SpineLocator(spine_item="OEBPS/chapter-3.xhtml", chapter="Recovery"),
        CueLocator(start_seconds=61.5, end_seconds=90.0),
    ],
    ids=["section", "section_untitled", "page", "spine", "spine_chapter", "cue"],
)
async def test_each_locator_shape_comes_back_with_the_source_to_cite(
    world: _World, locator: Locator
) -> None:
    chunk_id = await world.chunk("Deload every fourth week.", _axis(0, lean=1), locator=locator)

    page = await world.search(KnowledgeSearchQuery(text="deload"))

    assert isinstance(page, KnowledgeSearchPage)
    (hit,) = page.hits
    assert hit.chunk_id == chunk_id
    assert hit.locator == locator
    assert (hit.source_id, hit.title, hit.author, hit.evidence_tier) == (
        world.source.id,
        world.source.title,
        "M. Kowalska",
        EvidenceTier.ARTICLE_OR_TRANSCRIPT,
    )
    assert (hit.text, hit.heading_path, hit.language) == (
        "Deload every fourth week.",
        ("Recovery",),
        SourceLanguage.EN,
    )
