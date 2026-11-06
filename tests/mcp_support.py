"""Exercise tools with nothing behind them, for tests of the server that don't exercise them
(ticket #401): each fake knows no exercise, so a tool call finds no match. And the real server
with `search_knowledge` over fixed passages (#424, #428)."""

from collections.abc import Mapping, Sequence
from uuid import UUID

from fastmcp import FastMCP

from ai_trainer.application.exercise_lookup import ExerciseFinder
from ai_trainer.application.exercise_resolution import ResolutionLimits
from ai_trainer.application.knowledge_search import KnowledgeSearch
from ai_trainer.application.training_history import ExerciseTrainingHistory, TrainingHistory
from ai_trainer.domain.embeddings import Embedded, EmbeddingOutcome
from ai_trainer.domain.exercises import (
    CatalogueExercise,
    CatalogueWrite,
    Exercise,
    ExerciseAlias,
    ExerciseCandidate,
    ExerciseDetail,
    ExerciseEntry,
    ExerciseLabel,
    ExerciseMeasure,
    LoggedName,
)
from ai_trainer.domain.knowledge_search import KnowledgeCandidates, KnowledgeFilters, KnowledgeHit
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.training import SessionSummary
from ai_trainer.mcp.server import build_mcp_server

EMBEDDING_MODEL = "qwen/qwen3-embedding-8b"
_LIMITS = ResolutionLimits(near_match=0.75, candidate_floor=0.25, shortlist_size=4)


class EmptyExercises:
    """Implements `ExerciseLookupPort` and `ExerciseRepositoryPort` over no exercises."""

    async def match_user_aliases(
        self, user_id: UUID, *, sport_id: str, names: Sequence[str], near: float
    ) -> Mapping[str, UUID]:
        return {}

    async def shortlist(
        self,
        user_id: UUID,
        *,
        sport_id: str,
        name: str,
        measure: str,
        floor: float,
        limit: int,
    ) -> Sequence[ExerciseCandidate]:
        return []

    async def labels(
        self, user_id: UUID, *, named: Mapping[str, UUID], near: float
    ) -> Mapping[str, ExerciseLabel]:
        return {}

    async def describe(
        self, user_id: UUID, exercise_ids: Sequence[UUID], *, lang: str
    ) -> Sequence[ExerciseDetail]:
        return []

    async def upsert_catalogue(self, exercises: Sequence[CatalogueExercise]) -> CatalogueWrite:
        raise NotImplementedError

    async def get(self, user_id: UUID, exercise_id: UUID) -> Exercise | None:
        return None

    async def create_custom(
        self, user_id: UUID, *, sport_id: str, name: str, default_measure: ExerciseMeasure
    ) -> Exercise:
        raise NotImplementedError

    async def add_user_alias(
        self, user_id: UUID, exercise_id: UUID, alias: str
    ) -> ExerciseAlias | None:
        raise NotImplementedError

    async def user_aliases(self, user_id: UUID) -> Sequence[ExerciseAlias]:
        return []


class EmptyExerciseHistory:
    async def entries(
        self, user_id: UUID, exercise_id: UUID, *, limit: int
    ) -> Sequence[ExerciseEntry]:
        return []

    async def typed_entries(
        self, user_id: UUID, typed_name: str, *, limit: int
    ) -> Sequence[ExerciseEntry]:
        return []

    async def logged_names(
        self, user_id: UUID, name: str, *, near: float, limit: int
    ) -> Sequence[LoggedName]:
        return []


def no_exercise_tools() -> tuple[ExerciseFinder, ExerciseTrainingHistory]:
    return (
        ExerciseFinder(EmptyExercises(), _LIMITS, EmptyExerciseHistory()),
        ExerciseTrainingHistory(EmptyExercises(), EmptyExerciseHistory()),
    )


class QueryEmbeddings:
    """`search_knowledge`'s query embedding, answering `outcome` and recording who acted."""

    def __init__(self, outcome: EmbeddingOutcome | None = None) -> None:
        self.outcome = outcome or Embedded(vectors=((1.0, 0.0),), model=EMBEDDING_MODEL)
        self.acting: list[UUID] = []

    async def embed_query(self, text: str, *, acting_user_id: UUID) -> EmbeddingOutcome:
        self.acting.append(acting_user_id)
        return self.outcome


class FixedPassages:
    """Both legs of the search, answering `hits` by meaning whatever the query."""

    def __init__(self, hits: Sequence[KnowledgeHit] = ()) -> None:
        self.hits = tuple(hits)

    async def candidates(
        self, *, vector: Sequence[float], query: str, filters: KnowledgeFilters, depth: int
    ) -> KnowledgeCandidates:
        return KnowledgeCandidates(by_meaning=self.hits, by_words=())


class NoSessions:
    async def list_sessions(self, *args: object, **kwargs: object) -> Sequence[SessionSummary]:
        return []


def knowledge_server(
    hits: Sequence[KnowledgeHit] = (), embeddings: QueryEmbeddings | None = None
) -> FastMCP:
    """The real server with `search_knowledge` over `hits` and no training data."""
    registry = default_sport_registry()
    return build_mcp_server(
        TrainingHistory(NoSessions(), registry),
        *no_exercise_tools(),
        knowledge=KnowledgeSearch(
            embeddings=embeddings or QueryEmbeddings(),
            repository=FixedPassages(hits),
            sports=registry,
            embedding_model=EMBEDDING_MODEL,
            depth=20,
        ),
    )
