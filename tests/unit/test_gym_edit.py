"""The gym plugin's draft edit (PRD-0004 F2, tickets #158 and #159): the athlete's exercise
name, number of sets, reps, load in kg and RIR replace each exercise's own, an untouched load
keeps what was typed, and a value left as shown touches no set (ticket #395)."""

import pytest
from pydantic import JsonValue, ValidationError

from ai_trainer.domain.sports.gym.edit import apply_gym_edit
from ai_trainer.domain.sports.gym.payload import GymPayload, GymSet, Load

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


def _as_v2(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return GymPayload.model_validate(payload).model_dump(mode="json")


def test_an_untouched_exercise_keeps_what_was_typed() -> None:
    edited = apply_gym_edit(
        _PAYLOAD,
        _exercises(
            {"name": "deadlift", "sets": "3", "reps": "5", "load": "142.88", "rir": "2"},
            {"name": "pull-ups", "sets": "3", "reps": "", "load": "", "rir": ""},
        ),
    )

    assert isinstance(edited, GymPayload)
    assert edited.model_dump(mode="json") == _as_v2(_PAYLOAD)


def test_a_changed_load_is_now_typed_in_kg() -> None:
    edited = apply_gym_edit(_PAYLOAD, _exercises({"load": " 150 "}))

    load = Load(original="150 kg", value=150, unit="kg", kg=150)
    assert [gym_set.load for gym_set in edited.activities[0].sets] == [load] * 3


def test_a_load_added_to_a_bodyweight_exercise_is_in_kg() -> None:
    edited = apply_gym_edit(_PAYLOAD, _exercises({}, {"load": "10"}))

    pull_ups = edited.activities[1]
    load = Load(original="10 kg", value=10, unit="kg", kg=10)
    assert [gym_set.load for gym_set in pull_ups.sets] == [load] * 3
    assert pull_ups.measure == "load_reps"


def test_changed_sets_reps_rir_and_name_replace_the_stored_ones() -> None:
    edited = apply_gym_edit(
        _PAYLOAD, _exercises({"name": " Sumo deadlift ", "sets": "4", "reps": "3", "rir": "0"})
    )

    exercise = edited.activities[0]
    assert (exercise.name_as_typed, exercise.rir) == ("Sumo deadlift", 0)
    assert [gym_set.reps for gym_set in exercise.sets] == [3, 3, 3, 3]
    assert {gym_set.load.original for gym_set in exercise.sets if gym_set.load} == {"315 lb"}


def test_a_blank_field_clears_it() -> None:
    edited = apply_gym_edit(_PAYLOAD, _exercises({"sets": "", "reps": " ", "load": "", "rir": ""}))

    exercise = edited.activities[0]
    assert exercise.sets == [GymSet()]
    assert (exercise.rir, exercise.measure) == (None, "reps")
    assert exercise.name_as_typed == "deadlift"


def test_rows_past_the_stored_exercises_and_unknown_fields_are_ignored() -> None:
    edited = apply_gym_edit(_PAYLOAD, _exercises({"tempo": "3-1-1"}, {}, {"sets": "lots"}))

    assert edited.model_dump(mode="json") == _as_v2(_PAYLOAD)


def test_an_edit_without_rows_changes_nothing() -> None:
    assert apply_gym_edit(_PAYLOAD, {}).model_dump(mode="json") == _as_v2(_PAYLOAD)


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

    assert edited.model_dump(mode="json") == _as_v2(_TINY)


def test_a_word_typed_over_a_tiny_load_is_still_rejected() -> None:
    with pytest.raises(ValidationError) as raised:
        apply_gym_edit(_TINY, _exercises({"load": "heavy"}))

    assert [error["loc"] for error in raised.value.errors()] == [("activities", 0, "load")]


def test_a_typed_0_kg_load_is_still_rejected() -> None:
    with pytest.raises(ValidationError) as raised:
        apply_gym_edit(_PAYLOAD, _exercises({"load": "0"}))

    assert [error["loc"] for error in raised.value.errors()] == [("activities", 0, "load")]


_MEASURED: dict[str, JsonValue] = {
    "sport": "gym",
    "activities": [
        {
            "name_as_typed": "plank",
            "measure": "time",
            "sets": [
                {"duration_s": 45, "band_label": "red"},
                {"duration_s": 60, "band_label": "red", "reps": 8},
            ],
        }
    ],
}


def test_a_save_left_as_shown_keeps_time_band_and_a_set_that_differs() -> None:
    # The form shows the first set until #398 edits set by set.
    edited = apply_gym_edit(
        _MEASURED, _exercises({"name": "plank", "sets": "2", "reps": "", "load": "", "rir": ""})
    )

    assert edited.model_dump(mode="json") == _as_v2(_MEASURED)


def test_more_sets_copy_the_last_and_fewer_cut_from_the_end() -> None:
    more = apply_gym_edit(_MEASURED, _exercises({"sets": "3"})).activities[0].sets
    fewer = apply_gym_edit(_MEASURED, _exercises({"sets": "1"})).activities[0].sets

    assert [gym_set.duration_s for gym_set in more] == [45, 60, 60]
    assert [gym_set.duration_s for gym_set in fewer] == [45]


def test_a_blank_set_count_leaves_one_set() -> None:
    edited = apply_gym_edit(_MEASURED, _exercises({"sets": ""}))

    assert [gym_set.duration_s for gym_set in edited.activities[0].sets] == [45]


def test_changed_reps_go_on_every_set_and_keep_time_and_band() -> None:
    edited = apply_gym_edit(_MEASURED, _exercises({"reps": "5"}))

    assert edited.activities[0].measure == "time"
    assert [(s.reps, s.duration_s, s.band_label) for s in edited.activities[0].sets] == [
        (5, 45, "red"),
        (5, 60, "red"),
    ]
