"""The committed `onboarding.goals`, `.level` and `.schedule` eval datasets (ADR-0009, ADR-0028,
ticket #255): each reads as its template's own deps and output, holds the ticket's AC answer, and
covers European forms, unclear and empty answers, and an instruction inside the answer."""

from datetime import date
from pathlib import Path

import pytest
from pydantic import BaseModel
from pydantic_evals import Dataset
from pydantic_evals.evaluators import Evaluator

from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.onboarding import (
    GoalsMatch,
    GoalsRead,
    LevelMatch,
    LevelRead,
    OnboardingDeps,
    ScheduleMatch,
    ScheduleRead,
)

_DATASETS = Path(__file__).parents[2] / "evals" / "datasets"
_STEPS: list[tuple[str, type[BaseModel], type[Evaluator[EvalCaseInputs, BaseModel]]]] = [
    ("onboarding.goals", GoalsRead, GoalsMatch),
    ("onboarding.level", LevelRead, LevelMatch),
    ("onboarding.schedule", ScheduleRead, ScheduleMatch),
]


def _dataset(
    template_id: str, output: type[BaseModel], grader: type[Evaluator[EvalCaseInputs, BaseModel]]
) -> Dataset[EvalCaseInputs, BaseModel, object]:
    return Dataset[EvalCaseInputs, output, object].from_file(  # type: ignore[valid-type]
        _DATASETS / f"{template_id}.yaml", custom_evaluator_types=[grader]
    )


@pytest.mark.parametrize(("template_id", "output", "grader"), _STEPS)
def test_every_case_carries_valid_deps_and_an_expected_read(
    template_id: str,
    output: type[BaseModel],
    grader: type[Evaluator[EvalCaseInputs, BaseModel]],
) -> None:
    dataset = _dataset(template_id, output, grader)
    for case in dataset.cases:
        OnboardingDeps.model_validate(case.inputs.deps)
        assert isinstance(case.expected_output, output), case.name


@pytest.mark.parametrize(("template_id", "output", "grader"), _STEPS)
def test_every_dataset_has_ten_cases_graded_in_code_with_the_angles_covered(
    template_id: str,
    output: type[BaseModel],
    grader: type[Evaluator[EvalCaseInputs, BaseModel]],
) -> None:
    dataset = _dataset(template_id, output, grader)
    names = [str(case.name) for case in dataset.cases]
    assert len(names) >= 10
    for angle in ("empty", "safety"):
        assert any(name.startswith(f"{angle}:") for name in names), angle
    assert [type(evaluator) for evaluator in dataset.evaluators] == [grader]


def _case(template_id: str, prompt: str) -> BaseModel:
    step = next(step for step in _STEPS if step[0] == template_id)
    [case] = [case for case in _dataset(*step).cases if case.inputs.prompt == prompt]
    assert case.expected_output is not None
    return case.expected_output


def test_by_june_sent_in_october_is_expected_in_june_next_year() -> None:
    expected = _case("onboarding.goals", "Send my first 7c outdoors by June")

    assert isinstance(expected, GoalsRead)
    assert expected.converted(today=date(2026, 10, 25))["goals"] == [
        {"text": "Send my first 7c outdoors", "target_date": "2027-06-30"}
    ]


def test_the_schedule_ac_answer_is_expected_as_4_sessions_of_120_minutes() -> None:
    expected = _case("onboarding.schedule", "4 times a week, around 2 hours, Mon Wed Fri Sat")

    assert isinstance(expected, ScheduleRead)
    assert expected.converted() == {
        "sessions_per_week": 4,
        "session_minutes": 120,
        "preferred_days": [0, 2, 4, 5],
    }


def test_the_level_ac_answer_is_expected_with_the_lift_under_level() -> None:
    expected = _case("onboarding.level", "Deadlift 125 kg, push pull legs")

    assert expected == LevelRead(level="Deadlift 125 kg", exercises="Push pull legs")
