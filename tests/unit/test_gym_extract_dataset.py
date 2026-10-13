"""The committed `gym.extract` eval dataset covers the ticket's cases (ADR-0009, ticket #159)."""

from datetime import date
from pathlib import Path
from typing import Any

from pydantic_evals import Dataset

from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.extract import ActivitiesMatch, DateMatch, ExtractDeps

_ROOT = Path(__file__).parents[2] / "evals"
_DATASET = _ROOT / "datasets" / "gym.extract.yaml"
_BASELINE = _ROOT / "baselines" / "gym.extract.json"


def _dataset() -> Dataset[EvalCaseInputs, GymExtraction, Any]:
    return Dataset[EvalCaseInputs, GymExtraction, Any].from_file(
        _DATASET, custom_evaluator_types=(DateMatch, ActivitiesMatch)
    )


def _cases() -> dict[str, Any]:
    return {str(case.name): case for case in _dataset().cases}


def _expected(name: str) -> GymExtraction:
    expected = _cases()[name].expected_output
    assert isinstance(expected, GymExtraction)
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


def test_the_dataset_covers_dates_several_exercises_mixed_sports_units_and_polish() -> None:
    names = " ".join(_cases())

    for kind in (
        "relative:",
        "stated:",
        "assumed:",
        "multi-activity:",
        "mixed sport:",
        "units:",
        "rir:",
        "bodyweight:",
        "polish:",
        "ambiguous:",
    ):
        assert kind in names, kind


def test_every_exercise_name_and_typed_load_is_in_its_message() -> None:
    for name, case in _cases().items():
        for exercise in _expected(name).activities:
            assert exercise.name in case.inputs.prompt, name
            assert exercise.load is None or exercise.load.original in case.inputs.prompt, name


def test_5x5_squat_then_3_sets_of_pull_ups_is_both_exercises() -> None:
    name = "multi-activity: 5x5 squat then 3 sets of pull-ups (AC)"
    case = _cases()[name]
    squat, pull_ups = _expected(name).to_payload().activities

    assert case.inputs.prompt == "5x5 squat at 100kg, then 3 sets of pull-ups"
    assert (squat.name, squat.sets, squat.reps, squat.load and squat.load.kg) == (
        "squat",
        5,
        5,
        100,
    )
    assert (pull_ups.name, pull_ups.sets, pull_ups.reps, pull_ups.load) == (
        "pull-ups",
        3,
        None,
        None,
    )


def test_bench_yesterday_just_after_local_midnight_is_the_athletes_yesterday() -> None:
    """At 00:40 in Warsaw it is still the day before in UTC, so "yesterday" is only right in
    the athlete's timezone (ADR-0014)."""
    name = "relative: bench yesterday 3\N{MULTIPLICATION SIGN}8 @ 60 just after midnight (AC)"
    case = _cases()[name]
    expected = _expected(name)
    (bench,) = expected.to_payload().activities

    assert case.inputs.prompt == "Bench yesterday 3\N{MULTIPLICATION SIGN}8 @ 60"
    assert case.inputs.deps["now_local"] == "2026-10-13T00:40:00+02:00"
    assert (expected.occurred_on, expected.date_source) == (date(2026, 10, 12), "relative")
    assert bench.load is not None
    assert (bench.load.original, bench.load.unit, bench.load.kg) == ("60", "kg", 60)


def test_a_deadlift_in_lb_is_stored_in_kg_with_its_original() -> None:
    (deadlift,) = _expected("units: deadlift in lb (AC)").to_payload().activities

    assert deadlift.load is not None
    assert (deadlift.load.original, deadlift.load.unit, deadlift.load.kg) == (
        "315 lb",
        "lb",
        142.88,
    )


def test_the_baseline_is_committed() -> None:
    assert _BASELINE.is_file()
