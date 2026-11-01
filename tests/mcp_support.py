"""Exercise tools with nothing behind them, for tests of the server that don't exercise them
(ticket #401): each fake knows no exercise, so a tool call finds no match."""

from collections.abc import Mapping, Sequence
from uuid import UUID

from ai_trainer.application.exercise_lookup import ExerciseFinder
from ai_trainer.application.exercise_resolution import ResolutionLimits
from ai_trainer.application.training_history import ExerciseTrainingHistory
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
)

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


def no_exercise_tools() -> tuple[ExerciseFinder, ExerciseTrainingHistory]:
    return (
        ExerciseFinder(EmptyExercises(), _LIMITS),
        ExerciseTrainingHistory(EmptyExercises(), EmptyExerciseHistory()),
    )
