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


def test_questions_keeps_only_the_named_questions_in_file_order() -> None:
    """A Jev eval check asks one rubric question out of a set (#124)."""
    decision = DecisionSetLoader(root=FIXTURES).load("sample", 1, questions={"effort", "sport"})

    assert list(decision.output_type.model_fields) == ["sport", "effort"]
    assert decision.output_type.__doc__ == _file()["goal"]


def test_questions_naming_a_question_the_set_lacks_are_refused() -> None:
    with pytest.raises(ValueError, match="mood"):
        DecisionSetLoader(root=FIXTURES).load("sample", 1, questions={"sport", "mood"})


def test_questions_keeping_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        DecisionSetLoader(root=FIXTURES).load("sample", 1, questions=set())


def test_runtime_options_for_a_question_left_out_are_refused() -> None:
    with pytest.raises(ValueError, match="sport"):
        DecisionSetLoader(root=FIXTURES).load(
            "sample", 1, questions={"effort"}, options={"sport": {"gym", "climbing"}}
        )


def test_versions_lists_a_sets_files_in_order(tmp_path: Path) -> None:
    """The eval CLI names them when asked for one that has no file (#208)."""
    (tmp_path / "sample").mkdir()
    for name in ("v10.en.yaml", "v2.en.yaml", "v2.de.yaml", "notes.md"):
        (tmp_path / "sample" / name).write_text(SAMPLE_FILE.read_text())

    assert DecisionSetLoader(root=tmp_path).versions("sample") == [2, 10]
    assert DecisionSetLoader(root=tmp_path).versions("missing") == []


def _entry_property(**added: str) -> dict[str, Any]:
    decision = DecisionSetLoader(root=FIXTURES).load(
        "open_sample", 1, added_options={"entry": added}, questions={"entry"}
    )
    properties: dict[str, Any] = decision.output_type.model_json_schema()["properties"]
    return dict(properties["entry"])


def test_an_open_choice_lists_the_added_options_before_the_files_own() -> None:
    prop = _entry_property(first="The first entry.", second="The second entry.")

    assert [(option["const"], option["description"]) for option in prop["anyOf"]] == [
        ("first", "The first entry."),
        ("second", "The second entry."),
        ("none", "None of the other entries is what the athlete meant."),
    ]


def test_an_open_choice_with_one_added_option_has_two() -> None:
    assert [option["const"] for option in _entry_property(only="The one.")["anyOf"]] == [
        "only",
        "none",
    ]


def test_an_open_choice_with_nothing_added_is_refused() -> None:
    with pytest.raises(ValueError, match="at least two"):
        DecisionSetLoader(root=FIXTURES).load("open_sample", 1, questions={"entry"})


def test_options_added_to_a_closed_choice_are_refused() -> None:
    with pytest.raises(ValueError, match="closed"):
        DecisionSetLoader(root=FIXTURES).load(
            "open_sample",
            1,
            questions={"closed"},
            added_options={"closed": {"green": "A green thing."}},
        )


def test_options_added_to_a_question_the_set_lacks_are_refused() -> None:
    with pytest.raises(ValueError, match="mood"):
        DecisionSetLoader(root=FIXTURES).load(
            "open_sample", 1, added_options={"mood": {"calm": "Calm."}}
        )


def test_an_added_option_named_like_a_file_option_is_refused() -> None:
    with pytest.raises(ValueError, match="none"):
        _entry_property(none="Clashes with the file's own.")


def test_a_closed_choice_still_needs_two_options_in_its_file(tmp_path: Path) -> None:
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "v1.en.yaml").write_text(
        "goal: g\nquestions:\n  q:\n    kind: choice\n    question: q\n    options:\n      a: A.\n"
    )

    with pytest.raises(ValidationError):
        DecisionSetLoader(root=tmp_path).load("bad", 1)
