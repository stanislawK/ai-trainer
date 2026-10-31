"""The gym plugin: payload v2 and its v1 upcast, load normalization and the extraction it is
built from (ADR-0006, ADR-0022, ADR-0032, tickets #159 and #395)."""

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from ai_trainer.domain.sports.gym.edit import apply_gym_edit
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.domain.sports.gym.normalize import normalize_load, to_kg
from ai_trainer.domain.sports.gym.payload import (
    GYM_PAYLOAD_VERSION,
    Exercise,
    GymPayload,
    GymSet,
    Load,
)
from ai_trainer.domain.sports.registry import default_sport_registry


@pytest.mark.parametrize(
    ("value", "unit", "kg"),
    [
        (100, "kg", 100.0),
        (225, "lb", 102.06),
        (45, "lb", 20.41),
        (62.5, "kg", 62.5),
        (1, "lb", 0.45),
    ],
)
def test_a_load_is_converted_to_kg_in_code(value: float, unit: Any, kg: float) -> None:
    assert to_kg(value, unit) == kg


def test_a_load_in_lb_is_stored_in_kg_with_its_original_text() -> None:
    assert normalize_load(" 225 lbs ", 225, "lb") == Load(
        original="225 lbs", value=225, unit="lb", kg=102.06
    )


def test_a_tiny_load_that_rounds_to_0_kg_is_still_kept() -> None:
    assert normalize_load("0.001 lb", 0.001, "lb").kg == 0


def _extraction(**overrides: Any) -> GymExtraction:
    values: dict[str, Any] = {
        "occurred_on": "2026-10-02",
        "date_source": "assumed_today",
        "activities": [
            {
                "name": "squat",
                "sets": 5,
                "reps": 5,
                "load": {"original": "100kg", "value": 100, "unit": "kg"},
            },
            {"name": "pull-ups", "sets": 3},
        ],
    }
    return GymExtraction.model_validate(values | overrides)


def test_5x5_squat_at_100kg_then_3_sets_of_pull_ups_becomes_two_exercises() -> None:
    payload = _extraction().to_payload()

    assert payload.sport == "gym"
    squat = GymSet(reps=5, load=Load(original="100kg", value=100, unit="kg", kg=100))
    assert payload.activities == [
        Exercise(name_as_typed="squat", measure="load_reps", sets=[squat] * 5),
        Exercise(name_as_typed="pull-ups", measure="reps", sets=[GymSet()] * 3),
    ]


def test_a_load_in_lb_becomes_kg_and_keeps_what_was_typed() -> None:
    payload = _extraction(
        activities=[
            {
                "name": "deadlift",
                "sets": 1,
                "reps": 5,
                "load": {"original": "315 lb", "value": 315, "unit": "lb"},
                "rir": 2,
            }
        ]
    ).to_payload()

    (exercise,) = payload.activities
    assert exercise.sets[0].load == Load(original="315 lb", value=315, unit="lb", kg=142.88)
    assert exercise.rir == 2


def test_a_load_with_no_unit_is_read_as_kg_and_keeps_its_number_as_typed() -> None:
    # Owner-chosen at #159's plan gate: "Bench 3x8 @ 60" is 60 kg, editable before Confirm.
    payload = _extraction(
        activities=[
            {"name": "bench", "sets": 3, "reps": 8, "load": {"original": "60", "value": 60}}
        ]
    ).to_payload()

    (exercise,) = payload.activities
    assert exercise.sets[0].load == Load(original="60", value=60, unit="kg", kg=60)


def test_an_exercise_name_is_kept_as_typed_without_surrounding_space() -> None:
    payload = _extraction(activities=[{"name": "  Romanian deadlift "}]).to_payload()

    assert payload.activities == [
        Exercise(name_as_typed="Romanian deadlift", measure="reps", sets=[GymSet()])
    ]


def test_extraction_dates_are_absolute() -> None:
    extraction = _extraction(occurred_on="2026-10-01", date_source="relative")

    assert (extraction.occurred_on, extraction.date_source) == (date(2026, 10, 1), "relative")


@pytest.mark.parametrize(
    "exercise",
    [
        {"name": ""},
        {"name": "   "},
        {"name": "squat", "sets": 0},
        {"name": "squat", "reps": 0},
        {"name": "squat", "rir": -1},
        {"name": "squat", "rir": 11},
        {"name": "squat", "load": {"original": "0 kg", "value": 0, "unit": "kg"}},
        {"name": "squat", "load": {"original": "100 st", "value": 100, "unit": "st"}},
        {"name": "squat", "load": {"original": "", "value": 100, "unit": "kg"}},
        {"name": "squat", "implements": 0},
        {"name": "squat", "implements": 5},
        {"name": "squat", "band": "  "},
        {"name": "plank", "duration": {"value": 0, "unit": "s"}},
        {"name": "plank", "duration": {"value": 45, "unit": "h"}},
        {"name": "plank", "duration": {"value": 86401, "unit": "s"}},
        {"name": "plank", "duration": {"value": 1441, "unit": "min"}},
        {"name": "row", "distance": {"value": 10000, "unit": "km"}},
        {"name": "squat", "sets": 101},
        {"name": "row", "distance": {"value": 0, "unit": "m"}},
        {"name": "row", "distance": {"value": 1, "unit": "mi"}},
    ],
)
def test_extraction_rejects_invalid_exercises(exercise: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _extraction(activities=[exercise])


def test_extraction_needs_at_least_one_exercise() -> None:
    with pytest.raises(ValidationError):
        _extraction(activities=[])


def test_payload_is_version_two_and_tagged_gym() -> None:
    assert GYM_PAYLOAD_VERSION == 2
    payload = GymPayload.model_validate(
        {"activities": [{"name_as_typed": "pull-ups", "measure": "reps", "sets": [{"reps": 8}]}]}
    )

    assert payload.model_dump(mode="json") == {
        "sport": "gym",
        "activities": [
            {
                "exercise_id": None,
                "name_as_typed": "pull-ups",
                "measure": "reps",
                "per_side": False,
                "implements": None,
                "rir": None,
                "sets": [
                    {
                        "reps": 8,
                        "load": None,
                        "duration_s": None,
                        "distance_m": None,
                        "band_label": None,
                    }
                ],
            }
        ],
    }


def test_a_v1_exercise_reads_as_v2() -> None:
    load = {"original": "45kg", "value": 45, "unit": "kg", "kg": 45}
    payload = GymPayload.model_validate(
        {"activities": [{"name": "Bench", "sets": 2, "reps": 8, "load": load, "rir": 1}]}
    )

    gym_set = GymSet(reps=8, load=Load.model_validate(load))
    assert payload.activities == [
        Exercise(name_as_typed="Bench", measure="load_reps", rir=1, sets=[gym_set, gym_set])
    ]


@pytest.mark.parametrize(
    ("fields", "measure"),
    [
        ({"sets": 3, "duration": {"value": 45, "unit": "s"}}, "time"),
        ({"distance": {"value": 40, "unit": "m"}}, "distance"),
        ({"sets": 3, "reps": 10, "band": "red"}, "band_reps"),
        ({"reps": 5, "load": {"original": "6 kg", "value": 6, "unit": "kg"}}, "load_reps"),
        ({"sets": 4}, "reps"),
        # A weighted plank is held for time, and its load is still kept.
        (
            {
                "duration": {"value": 1, "unit": "min"},
                "load": {"original": "10 kg", "value": 10, "unit": "kg"},
            },
            "time",
        ),
    ],
)
def test_measure_comes_from_what_the_log_gave(fields: dict[str, Any], measure: str) -> None:
    payload = _extraction(activities=[{"name": "x", **fields}]).to_payload()

    assert payload.activities[0].measure == measure


def test_a_duration_in_minutes_and_a_distance_in_km_are_converted_in_code() -> None:
    payload = _extraction(
        activities=[
            {"name": "plank", "duration": {"value": 1.5, "unit": "min"}},
            {"name": "row", "distance": {"value": 1.5, "unit": "km"}},
        ]
    ).to_payload()

    assert payload.activities[0].sets[0].duration_s == 90
    assert payload.activities[1].sets[0].distance_m == 1500


def test_each_side_implements_and_band_reach_the_payload() -> None:
    payload = _extraction(
        activities=[{"name": "rows", "sets": 2, "per_side": True, "implements": 2, "band": " red "}]
    ).to_payload()

    (exercise,) = payload.activities
    assert (exercise.per_side, exercise.implements) == (True, 2)
    assert [gym_set.band_label for gym_set in exercise.sets] == ["red", "red"]


@pytest.mark.parametrize(
    "exercise",
    [
        {"name": ""},
        {"name": "squat", "sets": 0},
        {"name": "squat", "load": {"original": "100 kg", "value": 100, "unit": "kg", "kg": -1}},
        {"name": "squat", "load": {"original": "100 kg", "value": 100, "unit": None, "kg": 100}},
        {"name": "squat", "rir": 11},
        {"name_as_typed": "squat", "measure": "reps", "sets": []},
        {"name_as_typed": "squat", "measure": "reps", "sets": [{}] * 101},
        {"name_as_typed": "plank", "measure": "time", "sets": [{"duration_s": 86401}]},
        {"name_as_typed": "row", "measure": "distance", "sets": [{"distance_m": 1e7}]},
        {"name": "squat", "sets": 101},
        {"name_as_typed": "squat", "measure": "weight", "sets": [{}]},
        {"name_as_typed": "squat", "measure": "reps", "implements": 5, "sets": [{}]},
    ],
)
def test_payload_rejects_invalid_exercises(exercise: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        GymPayload.model_validate({"activities": [exercise]})


def test_payload_needs_at_least_one_exercise() -> None:
    with pytest.raises(ValidationError):
        GymPayload.model_validate({"activities": []})


def test_the_registry_wires_gym_to_its_extraction_template_and_payload() -> None:
    plugin = default_sport_registry().get("gym")

    assert (plugin.icon, plugin.label_key) == ("dumbbell", "sport.gym.label")
    assert plugin.extraction is not None
    assert (plugin.extraction.template_id, plugin.extraction.version) == ("gym.extract", 5)
    assert plugin.extraction.output_type is GymExtraction
    assert plugin.extraction.payload_type is GymPayload
    assert plugin.extraction.payload_version == GYM_PAYLOAD_VERSION
    assert plugin.extraction.apply_edit is apply_gym_edit
