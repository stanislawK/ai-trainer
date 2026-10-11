"""The `safety_screen` decision set and its eval dataset (ADR-0020 invariant 6, #120)."""

from pathlib import Path
from typing import Any

from pydantic_evals import Case, Dataset

from ai_trainer.llm.decisions.loader import DecisionSetLoader
from ai_trainer.llm.evals.runner import EvalCaseInputs

_DATASET = Path(__file__).parents[2] / "evals" / "datasets" / "safety_screen.yaml"


def _cases() -> list[Case[EvalCaseInputs, object, object]]:
    return list(Dataset[EvalCaseInputs, object, object].from_file(_DATASET).cases)


def _labels(case: Case[EvalCaseInputs, object, object]) -> dict[str, Any]:
    assert isinstance(case.expected_output, dict), case.name
    return case.expected_output


def test_v1_asks_pain_as_a_probability_and_severity_on_a_four_level_rubric() -> None:
    output_type = DecisionSetLoader().load("safety_screen", 1).output_type
    schema = output_type.model_json_schema()["properties"]

    assert list(schema) == ["pain", "severity"]
    assert (schema["pain"]["type"], schema["pain"]["minimum"], schema["pain"]["maximum"]) == (
        "number",
        0,
        1,
    )
    assert [level["const"] for level in schema["severity"]["anyOf"]] == [0, 1, 2, 3]


def test_the_dataset_has_at_least_twenty_cases_each_labelling_pain() -> None:
    cases = _cases()

    assert len(cases) >= 20
    assert all(isinstance(_labels(case).get("pain"), bool) for case in cases)
    labels = [_labels(case)["pain"] for case in cases]
    assert labels.count(True) >= 8
    assert labels.count(False) >= 8


def test_labels_name_only_the_sets_questions_with_valid_values() -> None:
    for case in _cases():
        labels = _labels(case)
        assert set(labels) <= {"pain", "severity"}, case.name
        if "severity" in labels:
            assert labels["severity"] in range(4), case.name


def test_the_played_down_knee_counts_as_pain() -> None:
    """The ticket's edge case: a joked-about soreness is still pain (G3)."""
    [knee] = [case for case in _cases() if case.inputs.prompt == "knee's a bit sore lol, anyway hi"]

    assert _labels(knee)["pain"] is True
