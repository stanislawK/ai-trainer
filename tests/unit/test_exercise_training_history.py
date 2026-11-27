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
    HistorySet,
)
from ai_trainer.domain.exercises import (
    Exercise,
    ExerciseEntry,
    ExerciseMeasure,
    LoggedName,
    LoggedSet,
)
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
        return self._read(limit)

    async def typed_entries(
        self, user_id: UUID, typed_name: str, *, limit: int
    ) -> Sequence[ExerciseEntry]:
        return self._read(limit)

    async def logged_names(
        self, user_id: UUID, name: str, *, near: float, limit: int
    ) -> Sequence[LoggedName]:
        return []

    def _read(self, limit: int) -> Sequence[ExerciseEntry]:
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
    assert result.exercise is not None
    assert result.exercise.name == "cable fly"
    assert str(_USER) not in result.model_dump_json()


async def test_a_typed_only_page_bounds_its_limit_and_names_no_exercise() -> None:
    history = _History(found=500)

    result = await ExerciseTrainingHistory(_Exercises(), history).typed_entries(
        _USER, "rising arms", limit=10_000
    )

    assert history.asked == [MAX_ENTRIES + 1]
    assert isinstance(result, ExerciseHistoryPage)
    assert result.exercise is None
    assert len(result.entries) == MAX_ENTRIES
    assert result.truncated is True


async def test_a_typed_name_with_no_entries_is_no_entries() -> None:
    result = await ExerciseTrainingHistory(_Exercises(), _History(found=0)).typed_entries(
        _USER, "rising arms"
    )

    assert result == ExerciseHistoryFailed(reason="no_entries")


async def test_an_exercise_the_athlete_cannot_see_is_not_found() -> None:
    result = await ExerciseTrainingHistory(EmptyExercises(), _History(found=1)).entries(
        _USER, uuid4()
    )

    assert result == ExerciseHistoryFailed(reason="not_found")


class _OneSet(_History):
    def __init__(self, logged: LoggedSet) -> None:
        super().__init__(found=1)
        self._logged = logged

    def _read(self, limit: int) -> Sequence[ExerciseEntry]:
        return [
            ExerciseEntry(
                session_id=uuid4(),
                occurred_on=date(2026, 10, 1),
                name_as_typed="cable fly",
                measure=ExerciseMeasure.LOAD_REPS,
                per_side=False,
                sets=(self._logged,),
            )
        ]


async def _first_set(logged: LoggedSet) -> dict[str, object]:
    result = await ExerciseTrainingHistory(_Exercises(), _OneSet(logged)).entries(
        _USER, _EXERCISE.id
    )
    assert isinstance(result, ExerciseHistoryPage)
    return result.entries[0].sets[0].model_dump(mode="json")


async def test_kg_set_dumps_only_set_reps_load() -> None:
    dumped = await _first_set(LoggedSet(set_no=1, reps=8, load_kg=45.0, load_original="45kg"))

    assert dumped == {"set_no": 1, "reps": 8, "load_kg": 45.0}


async def test_pound_set_keeps_load_original() -> None:
    dumped = await _first_set(LoggedSet(set_no=1, reps=8, load_kg=45.36, load_original="100 lb"))

    assert dumped == {"set_no": 1, "reps": 8, "load_kg": 45.36, "load_original": "100 lb"}


async def test_decimal_comma_kg_set_drops_load_original() -> None:
    dumped = await _first_set(LoggedSet(set_no=1, reps=8, load_kg=62.5, load_original="62,5 kg"))

    assert "load_original" not in dumped


def test_a_history_set_built_directly_dumps_without_nulls() -> None:
    assert HistorySet(set_no=2, pace=None).model_dump(mode="json") == {"set_no": 2}


def test_history_page_keeps_its_serialization_schema() -> None:
    schema = ExerciseHistoryPage.model_json_schema(mode="serialization")

    assert set(schema["properties"]) == {"exercise", "entries", "truncated"}
    assert "HistorySet" in schema["$defs"]


async def test_history_page_dump_has_no_session_id_or_null() -> None:
    typed = await ExerciseTrainingHistory(_Exercises(), _History(found=1)).typed_entries(
        _USER, "cable fly"
    )
    named = await ExerciseTrainingHistory(_Exercises(), _History(found=1)).entries(
        _USER, _EXERCISE.id
    )

    for page in (typed, named):
        assert isinstance(page, ExerciseHistoryPage)
        text = page.model_dump_json()
        assert "session_id" not in text
        assert "null" not in text
    assert isinstance(typed, ExerciseHistoryPage)
    assert "exercise" not in typed.model_dump(mode="json")
