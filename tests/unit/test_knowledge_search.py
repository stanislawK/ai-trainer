"""`search_knowledge`'s use case with fakes (ADR-0033 *Retrieval*, #424): RRF at k = 60 over the
two legs, the filters it hands the repository, `limit` and `truncated`, and every failure as a
value, never a raise."""

from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.knowledge_search import KnowledgeSearch
from ai_trainer.domain.embeddings import (
    Embedded,
    EmbeddingFailed,
    EmbeddingFailure,
    EmbeddingOutcome,
    QueryTask,
    RateLimited,
)
from ai_trainer.domain.knowledge import EvidenceTier, SourceLanguage, SourceType
from ai_trainer.domain.knowledge_document import SectionLocator
from ai_trainer.domain.knowledge_search import (
    KnowledgeCandidates,
    KnowledgeFilters,
    KnowledgeHit,
    KnowledgeSearchFailed,
    KnowledgeSearchPage,
    KnowledgeSearchQuery,
    rrf_fuse,
)
from ai_trainer.domain.sports.registry import default_sport_registry

_ATHLETE = UUID("00000000-0000-4000-8000-000000000424")
_MODEL = "qwen/qwen3-embedding-8b"
_VECTOR = (0.5, 0.5)


def _hit(name: str) -> KnowledgeHit:
    return KnowledgeHit(
        chunk_id=uuid4(),
        text=f"passage {name}",
        heading_path=("Deload",),
        language=SourceLanguage.EN,
        source_id=uuid4(),
        title=f"Guide {name}",
        author="M. Kowalska",
        evidence_tier=EvidenceTier.COACHING_BOOK,
        source_type=SourceType.BOOK,
        source_url=None,
        attribution=None,
        locator=SectionLocator(section="Deload"),
        exercise_id=None,
    )


class _Embeddings:
    def __init__(self, outcome: EmbeddingOutcome | None = None) -> None:
        self.outcome = outcome or Embedded(vectors=(_VECTOR,), model=_MODEL)
        self.calls: list[tuple[str, UUID]] = []
        self.tasks: list[QueryTask] = []

    async def embed_query(
        self, text: str, *, acting_user_id: UUID, task: QueryTask = QueryTask.QUESTION
    ) -> EmbeddingOutcome:
        self.calls.append((text, acting_user_id))
        self.tasks.append(task)
        return self.outcome


class _Repository:
    def __init__(
        self, by_meaning: Sequence[KnowledgeHit] = (), by_words: Sequence[KnowledgeHit] = ()
    ) -> None:
        self.answer = KnowledgeCandidates(by_meaning=tuple(by_meaning), by_words=tuple(by_words))
        self.asked: list[tuple[tuple[float, ...], str, KnowledgeFilters, int]] = []

    async def candidates(
        self,
        *,
        vector: Sequence[float],
        query: str,
        filters: KnowledgeFilters,
        depth: int,
    ) -> KnowledgeCandidates:
        self.asked.append((tuple(vector), query, filters, depth))
        return self.answer


def _search(
    repository: _Repository, embeddings: _Embeddings | None = None, *, depth: int = 20
) -> KnowledgeSearch:
    return KnowledgeSearch(
        embeddings=embeddings or _Embeddings(),
        repository=repository,
        sports=default_sport_registry(),
        embedding_model=_MODEL,
        depth=depth,
    )


def test_rrf_scores_by_rank_at_k_60() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    # c: 1/61 + 1/62; a: 1/62 + 1/63; b: 1/61.
    assert rrf_fuse([[c, a], [b, c, a]]) == [c, a, b]
    # Two second places against one first place: a tie at k = 0 (1/2 + 1/2 = 1/1), so first
    # seen wins; at k = 60, 2/62 beats 1/61.
    assert rrf_fuse([[b, a], [c, a]], k=0) == [b, a, c]
    assert rrf_fuse([[b, a], [c, a]]) == [a, b, c]


def test_rrf_puts_a_passage_in_both_legs_above_one_in_either() -> None:
    both, meaning_only, words_only = uuid4(), uuid4(), uuid4()
    fused = rrf_fuse([[meaning_only, both], [words_only, both]])
    assert fused[0] == both
    assert set(fused) == {both, meaning_only, words_only}


def test_rrf_keeps_first_seen_order_on_a_tie() -> None:
    first, second = uuid4(), uuid4()
    assert rrf_fuse([[first], [second]]) == [first, second]


async def test_hits_are_fused_from_both_legs_best_first() -> None:
    both, meaning, words = _hit("both"), _hit("meaning"), _hit("words")
    repository = _Repository(by_meaning=[meaning, both], by_words=[words, both])

    page = await _search(repository).search(_ATHLETE, KnowledgeSearchQuery(text="deload"))

    assert isinstance(page, KnowledgeSearchPage)
    assert page.hits[0] == both
    assert {hit.chunk_id for hit in page.hits} == {both.chunk_id, meaning.chunk_id, words.chunk_id}
    assert page.truncated is False


async def test_a_question_is_embedded_as_a_question_unless_the_query_says_otherwise() -> None:
    embeddings = _Embeddings()
    search = _search(_Repository(by_words=[_hit("x")]), embeddings)

    await search.search(_ATHLETE, KnowledgeSearchQuery(text="how often to deload?"))
    await search.search(
        _ATHLETE, KnowledgeSearchQuery(text="rising straight arms", task=QueryTask.EXERCISE)
    )

    assert embeddings.tasks == [QueryTask.QUESTION, QueryTask.EXERCISE]


async def test_the_query_is_embedded_for_the_athlete_and_both_legs_get_the_filters() -> None:
    embeddings = _Embeddings()
    repository = _Repository(by_words=[_hit("x")])
    exercise = uuid4()

    await _search(repository, embeddings, depth=7).search(
        _ATHLETE,
        KnowledgeSearchQuery(
            text="  how often to deload?  ",
            sport_id="gym",
            topics=(" Deload ", "VOLUME", ""),
            exercise_id=exercise,
        ),
    )

    assert embeddings.calls == [("how often to deload?", _ATHLETE)]
    assert repository.asked == [
        (
            _VECTOR,
            "how often to deload?",
            KnowledgeFilters(
                embedding_model=_MODEL,
                sport_id="gym",
                topics=("deload", "volume"),
                exercise_id=exercise,
            ),
            7,
        )
    ]


async def test_the_page_is_cut_at_limit_and_says_so() -> None:
    hits = [_hit(str(i)) for i in range(4)]
    page = await _search(_Repository(by_meaning=hits)).search(
        _ATHLETE, KnowledgeSearchQuery(text="deload", limit=3)
    )
    assert isinstance(page, KnowledgeSearchPage)
    assert [hit.chunk_id for hit in page.hits] == [hit.chunk_id for hit in hits[:3]]
    assert page.truncated is True


async def test_no_match_is_a_failure() -> None:
    outcome = await _search(_Repository()).search(_ATHLETE, KnowledgeSearchQuery(text="deload"))
    assert outcome == KnowledgeSearchFailed(reason="no_match")


async def test_a_blank_query_fails_without_an_embedding_call() -> None:
    embeddings = _Embeddings()
    outcome = await _search(_Repository(), embeddings).search(
        _ATHLETE, KnowledgeSearchQuery(text="   ")
    )
    assert outcome == KnowledgeSearchFailed(reason="blank_query")
    assert embeddings.calls == []


async def test_an_unknown_sport_fails_with_the_known_sports_and_no_embedding_call() -> None:
    embeddings = _Embeddings()
    outcome = await _search(_Repository(), embeddings).search(
        _ATHLETE, KnowledgeSearchQuery(text="deload", sport_id="chess")
    )
    assert isinstance(outcome, KnowledgeSearchFailed)
    assert outcome.reason == "unknown_sport"
    assert set(outcome.known_sports) == {"climbing", "gym", "cycling"}
    assert embeddings.calls == []


@pytest.mark.parametrize(
    "outcome",
    [
        EmbeddingFailed(reason=EmbeddingFailure.TIMEOUT),
        EmbeddingFailed(reason=EmbeddingFailure.ERROR),
        EmbeddingFailed(reason=EmbeddingFailure.WRONG_SHAPE),
        RateLimited(retry_after_seconds=3),
        Embedded(vectors=(), model=_MODEL),
    ],
    ids=["timeout", "error", "wrong_shape", "rate_limited", "no_vector"],
)
async def test_a_failed_embedding_is_a_failure_and_reads_nothing(
    outcome: EmbeddingOutcome,
) -> None:
    repository = _Repository(by_words=[_hit("x")])
    result = await _search(repository, _Embeddings(outcome)).search(
        _ATHLETE, KnowledgeSearchQuery(text="deload")
    )
    assert result == KnowledgeSearchFailed(reason="embedding_unavailable")
    assert repository.asked == []
