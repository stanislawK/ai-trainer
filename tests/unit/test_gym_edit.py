"""The gym plugin's draft edit (PRD-0004 F2, tickets #158 and #159): the athlete's exercise
name, sets, reps, load in kg and RIR replace each exercise's own, an untouched load keeps what
was typed, and the result is validated by `GymPayload`."""

import pytest
from pydantic import JsonValue, ValidationError

from ai_trainer.domain.sports.gym.edit import apply_gym_edit
from ai_trainer.domain.sports.gym.payload import GymPayload, Load

_PAYLOAD: dict[str, JsonValue] = {
    "sport": "gym",
    "activities": [
        {
            "name": "deadlift",
            "sets": 3,
            "reps": 5,
            "load": {"original": "315 lb", "value": 315, "unit": "lb", "kg": 142.88},
            "rir": 2,
        },
        {"name": "pull-ups", "sets": 3, "reps": None, "load": None, "rir": None},
    ],
}


def _exercises(*rows: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {"activities": list(rows)}


def test_an_untouched_exercise_keeps_what_was_typed() -> None:
    edited = apply_gym_edit(
        _PAYLOAD,
        _exercises(
            {"name": "deadlift", "sets": "3", "reps": "5", "load": "142.88", "rir": "2"},
            {"name": "pull-ups", "sets": "3", "reps": "", "load": "", "rir": ""},
        ),
    )

    assert isinstance(edited, GymPayload)
    assert edited.model_dump(mode="json") == _PAYLOAD


def test_a_changed_load_is_now_typed_in_kg() -> None:
    edited = apply_gym_edit(_PAYLOAD, _exercises({"load": " 150 "}))

    assert edited.activities[0].load == Load(original="150 kg", value=150, unit="kg", kg=150)


def test_a_load_added_to_a_bodyweight_exercise_is_in_kg() -> None:
    edited = apply_gym_edit(_PAYLOAD, _exercises({}, {"load": "10"}))

    assert edited.activities[1].load == Load(original="10 kg", value=10, unit="kg", kg=10)


def test_changed_sets_reps_rir_and_name_replace_the_stored_ones() -> None:
    edited = apply_gym_edit(
        _PAYLOAD, _exercises({"name": " Sumo deadlift ", "sets": "4", "reps": "3", "rir": "0"})
    )

    exercise = edited.activities[0]
    assert (exercise.name, exercise.sets, exercise.reps, exercise.rir) == ("Sumo deadlift", 4, 3, 0)
    assert exercise.load is not None
    assert exercise.load.original == "315 lb"


def test_a_blank_field_clears_it() -> None:
    edited = apply_gym_edit(_PAYLOAD, _exercises({"sets": "", "reps": " ", "load": "", "rir": ""}))

    exercise = edited.activities[0]
    assert (exercise.sets, exercise.reps, exercise.load, exercise.rir) == (None, None, None, None)
    assert exercise.name == "deadlift"


def test_rows_past_the_stored_exercises_and_unknown_fields_are_ignored() -> None:
    edited = apply_gym_edit(_PAYLOAD, _exercises({"tempo": "3-1-1"}, {}, {"sets": "lots"}))

    assert edited.model_dump(mode="json") == _PAYLOAD


def test_an_edit_without_rows_changes_nothing() -> None:
    assert apply_gym_edit(_PAYLOAD, {}).model_dump(mode="json") == _PAYLOAD


@pytest.mark.parametrize(
    ("row", "field"),
    [
        ({"name": ""}, "activities.1.name"),
        ({"name": "   "}, "activities.1.name"),
        ({"sets": "0"}, "activities.1.sets"),
        ({"sets": "two"}, "activities.1.sets"),
        ({"reps": "0"}, "activities.1.reps"),
        ({"reps": "1.5"}, "activities.1.reps"),
        ({"load": "0"}, "activities.1.load"),
        ({"load": "heavy"}, "activities.1.load"),
        ({"rir": "-1"}, "activities.1.rir"),
        ({"rir": "11"}, "activities.1.rir"),
    ],
)
def test_a_rejected_value_is_located_where_the_form_names_it(
    row: dict[str, JsonValue], field: str
) -> None:
    with pytest.raises(ValidationError) as raised:
        apply_gym_edit(_PAYLOAD, _exercises({}, row))

    assert {".".join(str(part) for part in error["loc"]) for error in raised.value.errors()} == {
        field
    }


_TINY: dict[str, JsonValue] = {
    "sport": "gym",
    "activities": [
        {
            "name": "plate",
            "sets": None,
            "reps": None,
            "load": {"original": "0.001 lb", "value": 0.001, "unit": "lb", "kg": 0.0},
            "rir": None,
        }
    ],
}


def test_an_untouched_tiny_load_shown_as_0_kg_is_kept_not_rejected() -> None:
    # Skeptic finding at #159: a load that rounds to 0 kg showed as "0" and blocked any save.
    edited = apply_gym_edit(_TINY, _exercises({"name": "plate", "load": "0"}))

    assert edited.model_dump(mode="json") == _TINY


def test_a_word_typed_over_a_tiny_load_is_still_rejected() -> None:
    with pytest.raises(ValidationError) as raised:
        apply_gym_edit(_TINY, _exercises({"load": "heavy"}))

    assert [error["loc"] for error in raised.value.errors()] == [("activities", 0, "load")]


def test_a_typed_0_kg_load_is_still_rejected() -> None:
    with pytest.raises(ValidationError) as raised:
        apply_gym_edit(_PAYLOAD, _exercises({"load": "0"}))

    assert [error["loc"] for error in raised.value.errors()] == [("activities", 0, "load")]
