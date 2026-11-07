"""The shortlist's search by meaning over `search_knowledge` (ADR-0031, ticket #429): the gym's
catalogue chunks, each exercise once at its best passage, and every failed search a raise the
shortlist turns into today's list."""

from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.exercise_candidates import (
    ExerciseSearchError,
    KnowledgeExerciseSearch,
)
from ai_trainer.application.knowledge_search import KnowledgeSearch
from ai_trainer.domain.embeddings import (
    Embedded,
    EmbeddingFailed,
    EmbeddingFailure,
    EmbeddingOutcome,
)
from ai_trainer.domain.knowledge import EvidenceTier, SourceLanguage, SourceType
from ai_trainer.domain.knowledge_document import SectionLocator
from ai_trainer.domain.knowledge_search import (
    KnowledgeCandidates,
    KnowledgeFilters,
    KnowledgeHit,
)
from ai_trainer.domain.sports.registry import default_sport_registry

_ATHLETE = UUID("00000000-0000-4000-8000-000000000429")
_MODEL = "qwen/qwen3-embedding-8b"


def _hit(exercise_id: UUID | None, language: SourceLanguage = SourceLanguage.EN) -> KnowledgeHit:
    return KnowledgeHit(
        chunk_id=uuid4(),
        text="passage",
        heading_path=("Dumbbell Lateral Raise",),
        language=language,
        source_id=uuid4(),
        title="Exercise catalogue",
        author=None,
        evidence_tier=EvidenceTier.CATALOGUE,
        source_type=SourceType.CATALOGUE,
        source_url=None,
        attribution=None,
        locator=SectionLocator(section="Dumbbell Lateral Raise"),
        exercise_id=exercise_id,
    )


class _Embeddings:
    def __init__(self, outcome: EmbeddingOutcome) -> None:
        self.outcome = outcome
        self.asked: list[tuple[str, UUID]] = []

    async def embed_query(self, text: str, *, acting_user_id: UUID) -> EmbeddingOutcome:
        self.asked.append((text, acting_user_id))
        return self.outcome


class _Repository:
    def __init__(
        self, by_meaning: Sequence[KnowledgeHit], by_words: Sequence[KnowledgeHit] = ()
    ) -> None:
        self.answer = KnowledgeCandidates(by_meaning=tuple(by_meaning), by_words=tuple(by_words))
        self.filters: list[KnowledgeFilters] = []

    async def candidates(
        self, *, vector: Sequence[float], query: str, filters: KnowledgeFilters, depth: int
    ) -> KnowledgeCandidates:
        self.filters.append(filters)
        return self.answer


def _search(
    repository: _Repository, embeddings: _Embeddings | None = None
) -> KnowledgeExerciseSearch:
    return KnowledgeExerciseSearch(
        KnowledgeSearch(
            embeddings=embeddings or _Embeddings(Embedded(vectors=((0.5, 0.5),), model=_MODEL)),
            repository=repository,
            sports=default_sport_registry(),
            embedding_model=_MODEL,
            depth=20,
        )
    )


async def test_each_exercise_comes_once_at_its_best_passage() -> None:
    lateral, front = uuid4(), uuid4()
    # The English and the Polish chunk of one exercise are two hits and one exercise.
    repository = _Repository(
        [_hit(lateral), _hit(front), _hit(lateral, SourceLanguage.PL)], [_hit(front)]
    )

    assert await _search(repository).similar(_ATHLETE, "rising straight arms") == [lateral, front]


async def test_it_searches_the_gym_for_the_typed_name_as_the_athlete() -> None:
    embeddings = _Embeddings(Embedded(vectors=((0.5, 0.5),), model=_MODEL))
    repository = _Repository([_hit(uuid4())])

    await _search(repository, embeddings).similar(_ATHLETE, "rising straight arms")

    assert embeddings.asked == [("rising straight arms", _ATHLETE)]
    assert [f.sport_id for f in repository.filters] == ["gym"]


async def test_a_passage_with_no_exercise_is_left_out() -> None:
    lateral = uuid4()

    assert await _search(_Repository([_hit(None), _hit(lateral)])).similar(_ATHLETE, "x") == [
        lateral
    ]


@pytest.mark.parametrize(
    "outcome",
    [None, EmbeddingFailed(reason=EmbeddingFailure.TIMEOUT)],
    ids=["no match", "embedding unavailable"],
)
async def test_a_search_that_finds_nothing_raises_so_the_shortlist_falls_back(
    outcome: EmbeddingFailed | None,
) -> None:
    embeddings = _Embeddings(outcome or Embedded(vectors=((0.5, 0.5),), model=_MODEL))

    with pytest.raises(ExerciseSearchError):
        await _search(_Repository([]), embeddings).similar(_ATHLETE, "x")
