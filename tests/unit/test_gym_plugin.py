"""The gym plugin: payload v1, load normalization and the extraction it is built from
(ADR-0006, ADR-0022, ticket #159)."""

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
    assert payload.activities == [
        Exercise(
            name="squat",
            sets=5,
            reps=5,
            load=Load(original="100kg", value=100, unit="kg", kg=100),
        ),
        Exercise(name="pull-ups", sets=3),
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
    assert exercise.load == Load(original="315 lb", value=315, unit="lb", kg=142.88)
    assert exercise.rir == 2


def test_a_load_with_no_unit_is_read_as_kg_and_keeps_its_number_as_typed() -> None:
    # Owner-chosen at #159's plan gate: "Bench 3x8 @ 60" is 60 kg, editable before Confirm.
    payload = _extraction(
        activities=[
            {"name": "bench", "sets": 3, "reps": 8, "load": {"original": "60", "value": 60}}
        ]
    ).to_payload()

    (exercise,) = payload.activities
    assert exercise.load == Load(original="60", value=60, unit="kg", kg=60)


def test_an_exercise_name_is_kept_as_typed_without_surrounding_space() -> None:
    payload = _extraction(activities=[{"name": "  Romanian deadlift "}]).to_payload()

    assert payload.activities == [Exercise(name="Romanian deadlift")]


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
    ],
)
def test_extraction_rejects_invalid_exercises(exercise: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _extraction(activities=[exercise])


def test_extraction_needs_at_least_one_exercise() -> None:
    with pytest.raises(ValidationError):
        _extraction(activities=[])


def test_payload_is_version_one_and_tagged_gym() -> None:
    assert GYM_PAYLOAD_VERSION == 1
    payload = GymPayload.model_validate({"activities": [{"name": "pull-ups", "sets": 3}]})

    assert payload.model_dump(mode="json") == {
        "sport": "gym",
        "activities": [{"name": "pull-ups", "sets": 3, "reps": None, "load": None, "rir": None}],
    }


@pytest.mark.parametrize(
    "exercise",
    [
        {"name": ""},
        {"name": "squat", "sets": 0},
        {"name": "squat", "load": {"original": "100 kg", "value": 100, "unit": "kg", "kg": -1}},
        {"name": "squat", "load": {"original": "100 kg", "value": 100, "unit": None, "kg": 100}},
        {"name": "squat", "rir": 11},
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
    assert (plugin.extraction.template_id, plugin.extraction.version) == ("gym.extract", 1)
    assert plugin.extraction.output_type is GymExtraction
    assert plugin.extraction.payload_type is GymPayload
    assert plugin.extraction.payload_version == GYM_PAYLOAD_VERSION
    assert plugin.extraction.apply_edit is apply_gym_edit
