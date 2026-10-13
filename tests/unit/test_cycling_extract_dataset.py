"""The committed `cycling.extract` eval dataset covers the ticket's cases (ADR-0009, ticket
#160)."""

from datetime import date
from pathlib import Path
from typing import Any

from pydantic_evals import Dataset

from ai_trainer.domain.sports.cycling.extraction import CyclingExtraction
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.extract import ActivitiesMatch, DateMatch, ExtractDeps

_ROOT = Path(__file__).parents[2] / "evals"
_DATASET = _ROOT / "datasets" / "cycling.extract.yaml"
_BASELINE = _ROOT / "baselines" / "cycling.extract.json"


def _dataset() -> Dataset[EvalCaseInputs, CyclingExtraction, Any]:
    return Dataset[EvalCaseInputs, CyclingExtraction, Any].from_file(
        _DATASET, custom_evaluator_types=(DateMatch, ActivitiesMatch)
    )


def _cases() -> dict[str, Any]:
    return {str(case.name): case for case in _dataset().cases}


def _expected(name: str) -> CyclingExtraction:
    expected = _cases()[name].expected_output
    assert isinstance(expected, CyclingExtraction)
    return expected


def test_the_dataset_has_at_least_twenty_cases_graded_in_code() -> None:
    dataset = _dataset()

    assert len(dataset.cases) >= 20
    assert {type(evaluator) for evaluator in dataset.evaluators} == {DateMatch, ActivitiesMatch}
    assert all(case.expected_output is not None for case in dataset.cases)


def test_every_case_carries_valid_deps_with_its_message() -> None:
    for case in _dataset().cases:
        deps = ExtractDeps.model_validate(case.inputs.deps)
        assert deps.message == case.inputs.prompt, case.name
        assert deps.now_local.date() == deps.today, case.name
        assert deps.now_local.strftime("%A") == deps.weekday, case.name


def test_the_dataset_covers_dates_several_rides_mixed_sports_units_and_polish() -> None:
    names = " ".join(_cases())

    for kind in (
        "relative:",
        "stated:",
        "assumed:",
        "multi-activity:",
        "mixed sport:",
        "units:",
        "zones:",
        "polish:",
    ):
        assert kind in names, kind


def test_every_typed_distance_and_elevation_is_in_its_message() -> None:
    for name, case in _cases().items():
        for ride in _expected(name).activities:
            for typed in (ride.distance, ride.elevation):
                assert typed is None or typed.original in case.inputs.prompt, name


def test_62_km_with_800_m_this_morning_is_today_and_relative() -> None:
    name = "relative: 62 km with 800 m this morning (AC)"
    case = _cases()[name]
    (ride,) = _expected(name).to_payload().activities

    assert case.inputs.prompt == "Rode 62 km with 800 m climbing this morning"
    assert _expected(name).occurred_on.isoformat() == case.inputs.deps["today"]
    assert _expected(name).date_source == "relative"
    assert (ride.distance and ride.distance.km, ride.elevation and ride.elevation.m) == (62, 800)


def test_2_hours_easy_is_120_minutes_and_nothing_invented() -> None:
    (ride,) = _expected("assumed: 2 hours easy (AC)").to_payload().activities

    assert ride.moving_time_min == 120
    assert (ride.distance, ride.elevation, ride.zones) == (None, None, [])


def test_40_miles_yesterday_is_stored_in_km_with_its_original() -> None:
    expected = _expected("units: miles yesterday (AC)")
    (ride,) = expected.to_payload().activities

    assert (expected.occurred_on, expected.date_source) == (date(2026, 10, 12), "relative")
    assert ride.distance is not None
    assert (ride.distance.original, ride.distance.km) == ("40 miles", 64.37)


def test_the_baseline_is_committed() -> None:
    assert _BASELINE.is_file()
