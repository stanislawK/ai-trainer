from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from ai_trainer.llm.decisions.loader import (
    DECISIONS_ROOT,
    DecisionSetLoader,
    UnknownDecisionSetError,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "decisions"
SAMPLE_FILE = FIXTURES / "sample" / "v1.en.yaml"


def _file() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(SAMPLE_FILE.read_text())
    return loaded


def _schema_property(name: str) -> dict[str, Any]:
    decision = DecisionSetLoader(root=FIXTURES).load("sample", 1)
    properties: dict[str, Any] = decision.output_type.model_json_schema()["properties"]
    return dict(properties[name])


def test_the_set_carries_its_id_version_and_goal() -> None:
    decision = DecisionSetLoader(root=FIXTURES).load("sample", 1)

    assert decision.id == "sample"
    assert decision.version == 1
    assert decision.output_type.model_json_schema()["description"] == _file()["goal"]
    assert list(decision.output_type.model_fields) == ["mentions_pain", "sport", "effort"]


def test_a_probability_question_loads_as_a_bounded_float_asking_the_file_text() -> None:
    prop = _schema_property("mentions_pain")

    assert prop["type"] == "number"
    assert (prop["minimum"], prop["maximum"]) == (0, 1)
    assert prop["description"] == _file()["questions"]["mentions_pain"]["question"]


def test_a_choice_question_loads_each_option_with_its_description() -> None:
    prop = _schema_property("sport")
    spec = _file()["questions"]["sport"]

    assert prop["description"] == spec["question"]
    assert {option["const"]: option["description"] for option in prop["anyOf"]} == spec["options"]


def test_a_score_question_loads_its_levels_in_order_from_zero() -> None:
    prop = _schema_property("effort")
    spec = _file()["questions"]["effort"]

    assert prop["type"] == "integer"
    assert prop["description"] == spec["question"]
    assert [option["const"] for option in prop["anyOf"]] == [0, 1, 2]
    assert [option["description"] for option in prop["anyOf"]] == spec["levels"]


def test_the_output_type_validates_answers_and_rejects_out_of_range_ones() -> None:
    output_type = DecisionSetLoader(root=FIXTURES).load("sample", 1).output_type

    answer = output_type.model_validate({"mentions_pain": 0.2, "sport": "gym", "effort": 1})

    assert answer.model_dump() == {"mentions_pain": 0.2, "sport": "gym", "effort": 1}
    for bad in (
        {"mentions_pain": 1.5, "sport": "gym", "effort": 1},
        {"mentions_pain": 0.2, "sport": "rowing", "effort": 1},
        {"mentions_pain": 0.2, "sport": "gym", "effort": 3},
    ):
        with pytest.raises(ValidationError):
            output_type.model_validate(bad)


def test_runtime_options_keep_only_the_named_options_in_file_order() -> None:
    decision = DecisionSetLoader(root=FIXTURES).load(
        "sample", 1, options={"sport": {"cycling", "climbing"}}
    )

    prop = decision.output_type.model_json_schema()["properties"]["sport"]

    assert [option["const"] for option in prop["anyOf"]] == ["climbing", "cycling"]


def test_runtime_options_naming_an_option_the_file_lacks_are_refused() -> None:
    with pytest.raises(ValueError, match="rowing"):
        DecisionSetLoader(root=FIXTURES).load("sample", 1, options={"sport": {"gym", "rowing"}})


def test_runtime_options_leaving_fewer_than_two_are_refused() -> None:
    with pytest.raises(ValueError, match="two"):
        DecisionSetLoader(root=FIXTURES).load("sample", 1, options={"sport": {"gym"}})


def test_runtime_options_for_a_question_that_is_not_a_choice_are_refused() -> None:
    with pytest.raises(ValueError, match="effort"):
        DecisionSetLoader(root=FIXTURES).load("sample", 1, options={"effort": {"0", "1"}})


def test_runtime_options_for_a_question_the_set_lacks_are_refused() -> None:
    with pytest.raises(ValueError, match="discipline"):
        DecisionSetLoader(root=FIXTURES).load("sample", 1, options={"discipline": {"a", "b"}})


def test_a_missing_set_raises_naming_it() -> None:
    with pytest.raises(UnknownDecisionSetError, match=r"sample.*v9"):
        DecisionSetLoader(root=FIXTURES).load("sample", 9)


def test_a_question_of_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValidationError, match="free_text"):
        DecisionSetLoader(root=FIXTURES).load("broken", 1)


def test_the_default_root_is_the_decisions_package() -> None:
    package = Path(__file__).parents[2] / "src" / "ai_trainer" / "llm" / "decisions"

    assert package == DECISIONS_ROOT


@pytest.mark.parametrize(
    "questions",
    [
        {"q": {"kind": "probability", "question": "  "}},
        {"q": {"kind": "choice", "question": "Which?", "options": {"a": "", "b": "B."}}},
        {"q": {"kind": "choice", "question": "Which?", "options": {"a": "A.", "b": "   "}}},
        {"q": {"kind": "score", "question": "How hard?", "levels": ["", "Hard."]}},
    ],
)
def test_a_blank_question_option_or_level_is_refused(
    tmp_path: Path, questions: dict[str, Any]
) -> None:
    """Jev reads only the descriptions, and a blank level would quietly turn a score into a
    choice, so every one must say something."""
    (tmp_path / "blank").mkdir()
    (tmp_path / "blank" / "v1.en.yaml").write_text(
        yaml.safe_dump({"goal": "A note.", "questions": questions})
    )

    with pytest.raises(ValidationError):
        DecisionSetLoader(root=tmp_path).load("blank", 1)
