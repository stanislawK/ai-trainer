"""The committed `gym.extract` eval dataset covers the ticket's cases (ADR-0009, tickets #159 and
#206)."""

from datetime import date
from pathlib import Path
from typing import Any

from pydantic_evals import Dataset

from ai_trainer.domain.sports.gym.extraction import GymExtraction, GymQuestion
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.extract import ActivitiesMatch, DateMatch, ExtractDeps, QuestionMatch

_ROOT = Path(__file__).parents[2] / "evals"
_DATASET = _ROOT / "datasets" / "gym.extract.yaml"
_BASELINE = _ROOT / "baselines" / "gym.extract.json"


def _dataset() -> Dataset[EvalCaseInputs, GymExtraction | GymQuestion, Any]:
    return Dataset[EvalCaseInputs, GymExtraction | GymQuestion, Any].from_file(
        _DATASET, custom_evaluator_types=(DateMatch, ActivitiesMatch, QuestionMatch)
    )


def _cases() -> dict[str, Any]:
    return {str(case.name): case for case in _dataset().cases}


def _drafts() -> dict[str, Any]:
    return {
        name: case
        for name, case in _cases().items()
        if isinstance(case.expected_output, GymExtraction)
    }


def _expected(name: str) -> GymExtraction:
    expected = _cases()[name].expected_output
    assert isinstance(expected, GymExtraction)
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
    for name, case in _drafts().items():
        for exercise in _expected(name).activities:
            # A name the log left open is named as the recent sessions or the answer write it.
            named = [
                case.inputs.prompt,
                *case.inputs.deps.get("recent", []),
                *case.inputs.deps.get("answers", []),
            ]
            assert any(exercise.name in text for text in named), name
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


def _reading_cases() -> dict[str, Any]:
    return {name: case for name, case in _cases().items() if name.startswith("reading:")}


def test_the_reading_cases_ask_less_often_than_they_draft() -> None:
    """Asking has a cost: most reading cases are logs that something already settles (#206)."""
    cases = _reading_cases()
    asks = [name for name, case in cases.items() if isinstance(case.expected_output, GymQuestion)]

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
        assert isinstance(case.expected_output, GymExtraction), name
