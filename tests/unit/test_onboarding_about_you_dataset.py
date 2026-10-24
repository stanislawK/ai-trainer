"""The committed `onboarding.about_you` eval dataset (ADR-0009, ADR-0028, ticket #254): it reads
as the template's own deps and output, holds the ticket's AC answer, and covers units, unclear
details, empty answers, limits and an instruction inside the answer."""

from pathlib import Path

from pydantic_evals import Dataset

from ai_trainer.domain.athlete_profile import AboutYou
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.onboarding import AboutYouMatch, AboutYouRead, OnboardingDeps

_DATASET = Path(__file__).parents[2] / "evals" / "datasets" / "onboarding.about_you.yaml"


def _dataset() -> Dataset[EvalCaseInputs, AboutYouRead, object]:
    return Dataset[EvalCaseInputs, AboutYouRead, object].from_file(
        _DATASET, custom_evaluator_types=[AboutYouMatch]
    )


def test_every_case_carries_valid_deps_and_an_expected_read() -> None:
    for case in _dataset().cases:
        OnboardingDeps.model_validate(case.inputs.deps)
        assert isinstance(case.expected_output, AboutYouRead), case.name


def test_the_ac_answer_is_expected_as_31_178_cm_73_kg_and_the_shoulder() -> None:
    [case] = [
        case
        for case in _dataset().cases
        if case.inputs.prompt == "31, 5 ft 10, 160 lb, sore left shoulder"
    ]
    assert isinstance(case.expected_output, AboutYouRead)
    assert case.expected_output.converted() == AboutYou(
        age=31, height_cm=178, weight_kg=73, limits="Sore left shoulder"
    )


def test_the_dataset_covers_each_angle_with_at_least_ten_cases() -> None:
    names = [str(case.name) for case in _dataset().cases]
    assert len(names) >= 10
    for angle in ("units", "unclear", "empty", "limits", "safety"):
        assert any(name.startswith(f"{angle}:") for name in names), angle


def test_every_case_is_graded_in_code() -> None:
    assert [type(evaluator) for evaluator in _dataset().evaluators] == [AboutYouMatch]
