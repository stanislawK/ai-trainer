"""The gym question (B17, ADR-0015, ticket #206): a log `gym.extract` can't read safely asks
which movement an exercise name means, or which unit a load typed without one is in; code turns
the readings into options and enforces the pick. The exercises of the athlete's recent sessions,
with their units, are computed in code for the extraction's deps."""

from typing import Any

import pytest
from pydantic import ValidationError

from ai_trainer.domain.clarification import ChoiceOption
from ai_trainer.domain.sports.gym.extraction import GymExtraction, GymQuestion, recent_exercises
from ai_trainer.domain.sports.registry import default_sport_registry


def _movement(exercise: str, *movements: str) -> GymQuestion:
    return GymQuestion.model_validate(
        {"about": "movement", "exercise": exercise, "readings": list(movements)}
    )


def _unit(exercise: str, load: str, *units: str) -> GymQuestion:
    return GymQuestion.model_validate(
        {"about": "unit", "exercise": exercise, "load": load, "readings": list(units)}
    )


def test_each_movement_becomes_an_option_naming_it_likeliest_first() -> None:
    question = _movement("press", "overhead press", "bench press")

    assert question.options() == (
        ChoiceOption(
            id="overhead press",
            copy_key="gym_reading_movement",
            params={
                "exercise": "press",
                "movement": "overhead press",
                "others": "bench press",
            },
        ),
        ChoiceOption(
            id="bench press",
            copy_key="gym_reading_movement",
            params={
                "exercise": "press",
                "movement": "bench press",
                "others": "overhead press",
            },
        ),
    )


def test_a_movement_copied_from_the_recent_sessions_drops_their_unit_mark() -> None:
    """Skeptic finding: "overhead press (kg)" is the movement, not its name."""
    question = _movement("press", "Overhead press (kg)", "bench press (lb)")

    assert question.readings == ["overhead press", "bench press"]


def test_each_unit_becomes_an_option_with_the_load_as_typed() -> None:
    question = _unit("bench", "60", "lb", "kg")

    assert question.options() == (
        ChoiceOption(
            id="lb", copy_key="gym_reading_lb", params={"exercise": "bench", "load": "60"}
        ),
        ChoiceOption(
            id="kg", copy_key="gym_reading_kg", params={"exercise": "bench", "load": "60"}
        ),
    )


@pytest.mark.parametrize(
    "question",
    [
        {"about": "movement", "exercise": "press", "readings": ["overhead press"]},
        {"about": "movement", "exercise": "press", "readings": ["bench press", "Bench Press "]},
        {"about": "movement", "exercise": "press", "readings": ["bench press", " "]},
        {"about": "unit", "exercise": "bench", "load": "60", "readings": ["kg", "kg"]},
        {"about": "unit", "exercise": "bench", "load": "60", "readings": ["kg", "stone"]},
        {"about": "unit", "exercise": "bench", "readings": ["kg", "lb"]},
        {"about": "unit", "exercise": "bench", "load": " ", "readings": ["kg", "lb"]},
        {"about": "unit", "exercise": "bench", "load": "60kg", "readings": ["kg", "lb"]},
        {"about": "name", "exercise": "bench", "readings": ["a", "b"]},
    ],
    ids=[
        "one movement",
        "the same movement twice",
        "a blank movement",
        "the same unit twice",
        "not a unit",
        "a unit question without its load",
        "a blank load",
        "a load with its unit (skeptic finding)",
        "not a question the plugin asks",
    ],
)
def test_a_question_needs_two_distinct_readings_of_what_it_asks(question: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        GymQuestion.model_validate(question)


def _session(*exercises: tuple[str, str | None]) -> dict[str, Any]:
    """A recent session as the gym's `ActivityStore` reads it from its rows (ticket #376)."""
    return {"activities": [{"name": name, "unit": unit} for name, unit in exercises]}


def test_recent_exercises_are_each_once_with_their_unit_most_recent_session_first() -> None:
    assert recent_exercises(
        [
            _session(("bench press", "lb"), ("pull-ups", None)),
            _session(("squat", "kg"), ("bench press", "lb")),
            _session(("Squat", "kg"), ("bench press", "kg")),
        ]
    ) == ("bench press (lb)", "pull-ups", "squat (kg)", "bench press (kg)")


def test_no_recent_sessions_give_no_exercises() -> None:
    assert recent_exercises([]) == ()


def test_the_gym_plugin_asks_and_reads_recent_exercises() -> None:
    spec = default_sport_registry().get("gym").extraction

    assert spec is not None
    assert spec.question is not None
    assert spec.question.type is GymQuestion
    assert spec.question.since_version == 3
    assert spec.version >= spec.question.since_version
    assert spec.recent is recent_exercises


def _log(*exercises: tuple[str, str | None, str | None]) -> GymExtraction:
    return GymExtraction.model_validate(
        {
            "occurred_on": "2026-10-02",
            "date_source": "assumed_today",
            "activities": [
                {
                    "name": name,
                    "sets": 5,
                    "reps": 5,
                    "load": None
                    if original is None
                    else {"original": original, "value": 60, "unit": unit},
                }
                for name, original, unit in exercises
            ],
        }
    )


def test_a_movement_answer_names_the_exercises_typed_as_it_asked() -> None:
    """The tap is enforced in code, not only in the prompt (ADR-0015, #164 amendment)."""
    answered = _log(("press", "60", None), ("Press ", "50", None), ("squat", "100kg", "kg"))

    picked = _movement("press", "overhead press", "bench press").options()[1]

    assert [e.name for e in answered.answered(picked).activities] == [
        "bench press",
        "bench press",
        "squat",
    ]


def test_a_movement_answer_leaves_another_exercise_that_shares_a_word() -> None:
    """Asked about "press": a leg press in the same log is another exercise."""
    answered = _log(("press", "60", None), ("leg press", "100", None)).answered(
        _movement("press", "overhead press", "bench press").options()[0]
    )

    assert [e.name for e in answered.activities] == ["overhead press", "leg press"]


def test_a_unit_answer_sets_the_unit_of_every_load_typed_without_one() -> None:
    """A resumed run that put a unit on a load typed without one still takes the pick."""
    log = _log(("bench", "60", None), ("bench", "60", "kg"), ("row", "60", None))

    answered = log.answered(_unit("bench", "60", "kg", "lb").options()[1])

    loads = [e.load for e in answered.to_payload().activities]
    assert [(load.unit, load.kg) for load in loads if load is not None] == [("lb", 27.22)] * 3


def test_a_unit_answer_leaves_a_load_typed_with_its_unit() -> None:
    answered = _log(("bench", "60kg", "kg")).answered(_unit("bench", "60", "kg", "lb").options()[1])

    [exercise] = answered.activities
    assert exercise.load is not None
    assert exercise.load.unit == "kg"


@pytest.mark.parametrize(
    "answer",
    [
        ChoiceOption(id="climbing", copy_key="sport.climbing.label"),
        ChoiceOption(id="lb", copy_key="x"),
        ChoiceOption(id="overhead press", copy_key="x", params={"movement": "overhead press"}),
    ],
    ids=["a sport's option", "a unit without its load", "a movement without its exercise"],
)
def test_an_option_that_is_no_reading_changes_nothing(answer: ChoiceOption) -> None:
    log = _log(("press", "60", None))

    assert log.answered(answer) == log


def test_a_movement_answer_corrects_an_exercise_written_as_another_reading() -> None:
    """Skeptic finding: tapped *Overhead press*, a resumed run that still wrote "bench press"
    gets the pick."""
    picked = _movement("press", "overhead press", "bench press").options()[0]

    answered = _log(("Bench press", "60", None), ("squat", None, None)).answered(picked)

    assert [e.name for e in answered.activities] == ["overhead press", "squat"]
