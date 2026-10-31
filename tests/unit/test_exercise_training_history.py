"""`ExerciseTrainingHistory` (ADR-0010, ticket #401) with fakes: it bounds `limit` itself, so a
caller other than the MCP tool can't send the database a nonsense row count."""

from collections.abc import Sequence
from datetime import date
from uuid import UUID, uuid4

from ai_trainer.application.training_history import (
    MAX_ENTRIES,
    ExerciseHistoryFailed,
    ExerciseHistoryPage,
    ExerciseTrainingHistory,
)
from ai_trainer.domain.exercises import Exercise, ExerciseEntry, ExerciseMeasure
from tests.mcp_support import EmptyExercises

_USER = uuid4()
_EXERCISE = Exercise(
    id=uuid4(),
    sport_id="gym",
    owner_user_id=_USER,
    name="cable fly",
    default_measure=ExerciseMeasure.LOAD_REPS,
)


class _Exercises(EmptyExercises):
    async def get(self, user_id: UUID, exercise_id: UUID) -> Exercise | None:
        return _EXERCISE


class _History:
    def __init__(self, found: int) -> None:
        self._found = found
        self.asked: list[int] = []

    async def entries(
        self, user_id: UUID, exercise_id: UUID, *, limit: int
    ) -> Sequence[ExerciseEntry]:
        self.asked.append(limit)
        return [
            ExerciseEntry(
                session_id=uuid4(),
                occurred_on=date(2026, 10, 1),
                name_as_typed="cable fly",
                measure=ExerciseMeasure.LOAD_REPS,
                per_side=False,
                sets=(),
            )
            for _ in range(min(self._found, limit))
        ]


async def test_a_limit_below_one_still_reads_one_entry() -> None:
    history = _History(found=5)

    result = await ExerciseTrainingHistory(_Exercises(), history).entries(
        _USER, _EXERCISE.id, limit=0
    )

    assert history.asked == [2]
    assert isinstance(result, ExerciseHistoryPage)
    assert len(result.entries) == 1
    assert result.truncated is True


async def test_a_limit_above_the_maximum_is_cut_to_it() -> None:
    history = _History(found=500)

    result = await ExerciseTrainingHistory(_Exercises(), history).entries(
        _USER, _EXERCISE.id, limit=10_000
    )

    assert history.asked == [MAX_ENTRIES + 1]
    assert isinstance(result, ExerciseHistoryPage)
    assert len(result.entries) == MAX_ENTRIES


async def test_the_page_names_the_exercise_without_its_owner() -> None:
    result = await ExerciseTrainingHistory(_Exercises(), _History(found=1)).entries(
        _USER, _EXERCISE.id
    )

    assert isinstance(result, ExerciseHistoryPage)
    assert result.exercise.name == "cable fly"
    assert str(_USER) not in result.model_dump_json()


async def test_an_exercise_the_athlete_cannot_see_is_not_found() -> None:
    result = await ExerciseTrainingHistory(EmptyExercises(), _History(found=1)).entries(
        _USER, uuid4()
    )

    assert result == ExerciseHistoryFailed(reason="not_found")
