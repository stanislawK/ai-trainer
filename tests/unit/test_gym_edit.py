"""The gym plugin's draft edit (PRD-0010 B38, F2, tickets #158, #159, #395 and #398): the athlete
edits each exercise's name, measure, each side, implements and RIR, and every set's reps, load in
kg, time in seconds, distance in metres and band. A value left as shown keeps what was typed, a
blank one is cleared to None, and only the sets whose values changed differ."""

import pytest
from pydantic import JsonValue, ValidationError

from ai_trainer.domain.sports.gym.edit import apply_gym_edit
from ai_trainer.domain.sports.gym.payload import GymPayload, GymSet, Load

_LB: dict[str, JsonValue] = {"original": "315 lb", "value": 315, "unit": "lb", "kg": 142.88}

_PYRAMID: dict[str, JsonValue] = {
    "sport": "gym",
    "activities": [
        {
            "name_as_typed": "bench",
            "measure": "load_reps",
            "rir": 2,
            "sets": [
                {"reps": 10, "load": {"original": "60", "value": 60, "unit": "kg", "kg": 60}},
                {"reps": 8, "load": {"original": "70", "value": 70, "unit": "kg", "kg": 70}},
                {"reps": 6, "load": {"original": "80 kg", "value": 80, "unit": "kg", "kg": 80}},
            ],
        },
        {"name_as_typed": "plank", "measure": "time", "sets": [{"duration_s": 45}] * 3},
    ],
}

_DEADLIFT: dict[str, JsonValue] = {
    "sport": "gym",
    "activities": [
        {
            "name_as_typed": "deadlift",
            "measure": "load_reps",
            "sets": [{"reps": 5, "load": _LB}] * 3,
        }
    ],
}


def _set(**fields: str) -> dict[str, JsonValue]:
    return dict(fields)


def _form(*sets: dict[str, JsonValue], **exercise: JsonValue) -> dict[str, JsonValue]:
    return {"activities": [{**exercise, "sets": list(sets)}]}


def _shown(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """The form a card would post for the first exercise as it stands: every value as shown."""
    exercise = GymPayload.model_validate(payload).activities[0]
    return {
        "name": exercise.name_as_typed,
        "measure": exercise.measure,
        "per_side": "1" if exercise.per_side else "0",
        "implements": "" if exercise.implements is None else str(exercise.implements),
        "rir": "" if exercise.rir is None else str(exercise.rir),
        "sets": [
            {
                "reps": "" if s.reps is None else str(s.reps),
                "load": "" if s.load is None else f"{s.load.kg:g}",
                "duration": "" if s.duration_s is None else str(s.duration_s),
                "distance": "" if s.distance_m is None else f"{s.distance_m:g}",
                "band": s.band_label or "",
                "src": str(index),
            }
            for index, s in enumerate(exercise.sets)
        ],
    }


def test_a_form_left_as_shown_changes_nothing_and_keeps_what_was_typed() -> None:
    edited = apply_gym_edit(_DEADLIFT, {"activities": [_shown(_DEADLIFT)]})

    assert edited.model_dump(mode="json") == GymPayload.model_validate(_DEADLIFT).model_dump(
        mode="json"
    )
    assert edited.activities[0].sets[0].load == Load.model_validate(_LB)


def test_editing_one_set_of_a_pyramid_changes_only_that_set() -> None:
    form = _shown(_PYRAMID)
    sets = form["sets"]
    assert isinstance(sets, list)
    sets[1] = {**sets[1], "reps": "7", "load": "72.5"}  # type: ignore[dict-item]

    edited = apply_gym_edit(_PYRAMID, {"activities": [form]})

    before = GymPayload.model_validate(_PYRAMID).activities[0].sets
    after = edited.activities[0].sets
    assert after[0] == before[0]
    assert after[2] == before[2]
    assert (after[1].reps, after[1].load) == (
        7,
        Load(original="72.5 kg", value=72.5, unit="kg", kg=72.5),
    )


def test_an_exercise_the_form_leaves_out_stays_as_it_was() -> None:
    edited = apply_gym_edit(_PYRAMID, {"activities": [_shown(_PYRAMID)]})

    assert edited.activities[1] == GymPayload.model_validate(_PYRAMID).activities[1]


def test_a_cleared_value_is_none_not_zero() -> None:
    form = _shown(_PYRAMID)
    sets = form["sets"]
    assert isinstance(sets, list)
    sets[0] = {"reps": " ", "load": "", "duration": "", "distance": "", "band": ""}
    form["rir"] = ""

    edited = apply_gym_edit(_PYRAMID, {"activities": [form]}).activities[0]

    assert edited.sets[0] == GymSet()
    assert edited.rir is None


def test_time_distance_and_band_are_typed_in_s_m_and_a_label() -> None:
    form = _form(
        _set(reps="10", load="", duration="", distance="", band=" red "),
        _set(reps="", load="24", duration="45", distance="40.5", band=""),
        name="mix",
        measure="time",
        per_side="1",
        implements="2",
        rir="",
    )

    exercise = apply_gym_edit(_PYRAMID, {"activities": [{}, *form["activities"]]}).activities[1]  # type: ignore[misc]

    assert (exercise.name_as_typed, exercise.measure) == ("mix", "band_reps")
    assert (exercise.per_side, exercise.implements) == (True, 2)
    assert exercise.sets[0] == GymSet(reps=10, band_label="red")
    assert (exercise.sets[1].duration_s, exercise.sets[1].distance_m) == (45, 40.5)
    assert exercise.sets[1].load == Load(original="24 kg", value=24, unit="kg", kg=24)


def test_a_changed_measure_pick_stands_and_an_unchanged_one_follows_the_sets() -> None:
    blank = _set(reps="", load="", duration="", distance="", band="")
    picked = apply_gym_edit(_PYRAMID, _form(blank, measure="distance"))
    filled = apply_gym_edit(_PYRAMID, _form({**blank, "distance": "30"}, measure="distance"))
    kept_load = apply_gym_edit(_PYRAMID, {"activities": [{**_shown(_PYRAMID), "measure": "time"}]})
    cleared = apply_gym_edit(_PYRAMID, _form({**blank, "reps": "5"}, measure="load_reps"))

    assert picked.activities[0].measure == "distance"
    assert filled.activities[0].measure == "distance"
    # The load still shows for a time, so the pick isn't undone by the load a set keeps.
    assert kept_load.activities[0].measure == "time"
    # The stored measure picked again follows what the sets hold, here a cleared load.
    assert cleared.activities[0].measure == "reps"


def test_removing_a_middle_set_keeps_the_sets_after_it_as_they_were_typed() -> None:
    payload: dict[str, JsonValue] = {
        "sport": "gym",
        "activities": [
            {
                "name_as_typed": "deadlift",
                "measure": "load_reps",
                "sets": [{"reps": 5, "load": _LB}, {"reps": 3}, {"reps": 1, "load": _LB}],
            }
        ],
    }
    form = _shown(payload)
    rows = form["sets"]
    assert isinstance(rows, list)

    edited = apply_gym_edit(payload, {"activities": [{**form, "sets": [rows[0], rows[2]]}]})

    stored = GymPayload.model_validate(payload).activities[0].sets
    assert edited.activities[0].sets == [stored[0], stored[2]]


def test_a_row_with_a_blank_src_is_a_new_set_that_keeps_nothing_stored() -> None:
    form = _shown(_PYRAMID)
    rows = form["sets"]
    assert isinstance(rows, list)
    new = _set(reps="4", load="", duration="", distance="", band="", src="")

    edited = apply_gym_edit(_PYRAMID, {"activities": [{**form, "sets": [*rows, new]}]})

    assert edited.activities[0].sets[3] == GymSet(reps=4)


def test_a_typed_load_keeps_its_text_as_the_original() -> None:
    form = _form(_set(reps="5", load="1234567.5", duration="", distance="", band=""))

    edited = apply_gym_edit(_PYRAMID, form)

    load = edited.activities[0].sets[0].load
    assert load is not None
    assert (load.original, load.kg) == ("1234567.5 kg", 1234567.5)


def test_sets_can_be_added_and_removed() -> None:
    form = _shown(_PYRAMID)
    sets = form["sets"]
    assert isinstance(sets, list)
    added = apply_gym_edit(
        _PYRAMID, {"activities": [{**form, "sets": [*sets, _set(reps="4", load="90")]}]}
    ).activities[0]
    removed = apply_gym_edit(_PYRAMID, {"activities": [{**form, "sets": sets[:1]}]}).activities[0]

    assert [s.reps for s in added.sets] == [10, 8, 6, 4]
    assert [s.reps for s in removed.sets] == [10]


def test_every_set_removed_leaves_one_blank_set() -> None:
    edited = apply_gym_edit(_PYRAMID, {"activities": [{**_shown(_PYRAMID), "sets": []}]})

    assert edited.activities[0].sets == [GymSet()]


def test_a_form_with_no_sets_key_keeps_the_sets() -> None:
    edited = apply_gym_edit(_PYRAMID, {"activities": [{"name": "bench press"}]})

    exercise = edited.activities[0]
    assert exercise.name_as_typed == "bench press"
    assert exercise.sets == GymPayload.model_validate(_PYRAMID).activities[0].sets


def test_rows_past_the_stored_exercises_and_unknown_fields_are_ignored() -> None:
    edited = apply_gym_edit(_PYRAMID, {"activities": [{"tempo": "3-1-1"}, {}, {"name": "extra"}]})

    assert edited.model_dump(mode="json") == GymPayload.model_validate(_PYRAMID).model_dump(
        mode="json"
    )


def test_an_edit_without_rows_changes_nothing() -> None:
    assert apply_gym_edit(_PYRAMID, {}).model_dump(mode="json") == GymPayload.model_validate(
        _PYRAMID
    ).model_dump(mode="json")


def test_renaming_a_settled_exercise_makes_it_typed_only_again() -> None:
    settled = GymPayload.model_validate(_PYRAMID).model_dump(mode="json")
    activities = settled["activities"]
    assert isinstance(activities, list)
    first = activities[0]
    assert isinstance(first, dict)
    first["exercise_id"] = "7b0b1c2e-8c1e-4f3a-9a53-0d6a2f0c3b11"
    first["keep_name"] = True

    edited = apply_gym_edit(settled, {"activities": [{"name": "incline bench"}]})

    exercise = edited.activities[0]
    assert (exercise.exercise_id, exercise.keep_name) == (None, False)


@pytest.mark.parametrize(
    ("exercise", "sets", "field"),
    [
        ({"name": ""}, None, "activities.0.name"),
        ({"name": "   "}, None, "activities.0.name"),
        ({"measure": "tempo"}, None, "activities.0.measure"),
        ({"per_side": "maybe"}, None, "activities.0.per_side"),
        ({"implements": "0"}, None, "activities.0.implements"),
        ({"implements": "5"}, None, "activities.0.implements"),
        ({"rir": "11"}, None, "activities.0.rir"),
        ({}, {"reps": "0"}, "activities.0.sets.1.reps"),
        ({}, {"reps": "1.5"}, "activities.0.sets.1.reps"),
        ({}, {"load": "0"}, "activities.0.sets.1.load"),
        ({}, {"load": "heavy"}, "activities.0.sets.1.load"),
        ({}, {"duration": "-1"}, "activities.0.sets.1.duration"),
        ({}, {"duration": "86401"}, "activities.0.sets.1.duration"),
        ({}, {"distance": "far"}, "activities.0.sets.1.distance"),
    ],
)
def test_a_rejected_value_is_located_where_the_form_names_it(
    exercise: dict[str, JsonValue], sets: dict[str, JsonValue] | None, field: str
) -> None:
    form = _shown(_PYRAMID)
    form.update(exercise)
    if sets is not None:
        rows = form["sets"]
        assert isinstance(rows, list)
        rows[1] = {**rows[1], **sets}  # type: ignore[dict-item]

    with pytest.raises(ValidationError) as raised:
        apply_gym_edit(_PYRAMID, {"activities": [form]})

    assert {".".join(str(part) for part in error["loc"]) for error in raised.value.errors()} == {
        field
    }


def test_a_typed_load_of_zero_is_rejected_but_a_tiny_one_shown_as_zero_is_kept() -> None:
    tiny: dict[str, JsonValue] = {
        "sport": "gym",
        "activities": [
            {
                "name_as_typed": "plate",
                "measure": "load_reps",
                "sets": [
                    {"load": {"original": "0.001 lb", "value": 0.001, "unit": "lb", "kg": 0.0}}
                ],
            }
        ],
    }
    form: dict[str, JsonValue] = {"activities": [{"name": "plate", "sets": [{"load": "0"}]}]}

    assert apply_gym_edit(tiny, form).model_dump(mode="json") == GymPayload.model_validate(
        tiny
    ).model_dump(mode="json")
    with pytest.raises(ValidationError):
        apply_gym_edit(_PYRAMID, {"activities": [{"sets": [{"load": "0"}]}]})
    with pytest.raises(ValidationError) as raised:
        apply_gym_edit(tiny, {"activities": [{"sets": [{"load": "heavy"}]}]})
    assert [error["loc"] for error in raised.value.errors()] == [
        ("activities", 0, "sets", 0, "load")
    ]


def test_a_v1_draft_still_edits() -> None:
    v1: dict[str, JsonValue] = {
        "sport": "gym",
        "activities": [{"name": "row", "sets": 2, "reps": 8, "load": None, "rir": None}],
    }

    edited = apply_gym_edit(v1, {"activities": [{"name": "row", "sets": [{"reps": "9"}, {}]}]})

    assert [s.reps for s in edited.activities[0].sets] == [9, 8]
