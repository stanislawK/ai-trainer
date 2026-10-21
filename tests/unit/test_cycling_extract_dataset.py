"""The committed `cycling.extract` eval dataset covers the ticket's cases (ADR-0009, tickets
#160 and #206)."""

from datetime import date
from pathlib import Path
from typing import Any

from pydantic_evals import Dataset

from ai_trainer.domain.sports.cycling.extraction import CyclingExtraction, CyclingQuestion
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.extract import ActivitiesMatch, DateMatch, ExtractDeps, QuestionMatch

_ROOT = Path(__file__).parents[2] / "evals"
_DATASET = _ROOT / "datasets" / "cycling.extract.yaml"
_BASELINE = _ROOT / "baselines" / "cycling.extract.json"


def _dataset() -> Dataset[EvalCaseInputs, CyclingExtraction | CyclingQuestion, Any]:
    return Dataset[EvalCaseInputs, CyclingExtraction | CyclingQuestion, Any].from_file(
        _DATASET, custom_evaluator_types=(DateMatch, ActivitiesMatch, QuestionMatch)
    )


def _cases() -> dict[str, Any]:
    return {str(case.name): case for case in _dataset().cases}


def _drafts() -> dict[str, Any]:
    return {
        name: case
        for name, case in _cases().items()
        if isinstance(case.expected_output, CyclingExtraction)
    }


def _expected(name: str) -> CyclingExtraction:
    expected = _cases()[name].expected_output
    assert isinstance(expected, CyclingExtraction)
    return expected


def test_the_dataset_has_at_least_twenty_cases_graded_in_code() -> None:
    dataset = _dataset()

    assert len(dataset.cases) >= 20
    assert {type(evaluator) for evaluator in dataset.evaluators} == {
        DateMatch,
        ActivitiesMatch,
        QuestionMatch,
    }
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
    for name, case in _drafts().items():
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


def _reading_cases() -> dict[str, Any]:
    return {name: case for name, case in _cases().items() if name.startswith("reading:")}


def test_the_reading_cases_ask_less_often_than_they_draft() -> None:
    """Asking has a cost: most reading cases are logs that something already settles (#206)."""
    cases = _reading_cases()
    asks = [
        name for name, case in cases.items() if isinstance(case.expected_output, CyclingQuestion)
    ]

    assert len(cases) >= 12
    assert 0 < len(asks) < len(cases) / 2


def test_an_answered_case_always_expects_a_draft() -> None:
    answered = [name for name, case in _reading_cases().items() if case.inputs.deps.get("answers")]

    assert len(answered) >= 3
    for name in answered:
        _expected(name)


def test_the_cases_before_the_question_carry_no_recent_context_and_still_draft() -> None:
    """A clear log in the dataset still drafts, with no question (#206)."""
    for name, case in _cases().items():
        if name.startswith("reading:"):
            continue
        assert "recent" not in case.inputs.deps, name
        assert isinstance(case.expected_output, CyclingExtraction), name
