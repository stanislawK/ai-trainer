"""The committed chitchat eval dataset covers the ticket's cases and grades every reply for
tone, safety and diagnosis (ADR-0009, ticket #81)."""

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, LLMJudge

from ai_trainer.llm.chitchat import ChitchatDeps
from ai_trainer.llm.decisions.loader import DecisionSetLoader
from ai_trainer.llm.evals.jev_check import JevCheck
from ai_trainer.llm.evals.runner import EvalCaseInputs

_DATASET = Path(__file__).parents[2] / "evals" / "datasets" / "chitchat.yaml"


def _dataset() -> Dataset[EvalCaseInputs, str, object]:
    return Dataset[EvalCaseInputs, str, object].from_file(
        _DATASET, custom_evaluator_types=[JevCheck]
    )


def _cases(prefix: str) -> list[Case[EvalCaseInputs, str, object]]:
    return [case for case in _dataset().cases if str(case.name).startswith(f"{prefix}:")]


def _judge(evaluators: list[Evaluator[EvalCaseInputs, str, object]], name: str) -> LLMJudge:
    judges = [
        evaluator
        for evaluator in evaluators
        if isinstance(evaluator, LLMJudge)
        and isinstance(evaluator.assertion, dict)
        and evaluator.assertion.get("evaluation_name") == name
    ]
    assert len(judges) == 1, f"expected one {name!r} judge, found {len(judges)}"
    return judges[0]


def test_the_dataset_covers_greetings_history_unclear_and_pain() -> None:
    assert _cases("greeting")
    assert _cases("history")
    assert _cases("unclear")
    assert len(_cases("pain")) >= 3


def test_every_case_carries_valid_chitchat_deps_matching_its_prompt() -> None:
    for case in _dataset().cases:
        deps = ChitchatDeps.model_validate(case.inputs.deps)
        assert deps.message == case.inputs.prompt, case.name
        # `now_local` is a real local time in `timezone`, on `today` (ADR-0014).
        assert deps.now_local.astimezone(ZoneInfo(deps.timezone)) == deps.now_local, case.name
        assert (
            deps.now_local.utcoffset()
            == deps.now_local.astimezone(ZoneInfo(deps.timezone)).utcoffset()
        ), case.name
        assert deps.now_local.date() == deps.today, case.name


def test_a_history_case_carries_earlier_turns_and_is_judged_on_using_them() -> None:
    for case in _cases("history"):
        assert ChitchatDeps.model_validate(case.inputs.deps).history, case.name
        _judge(case.evaluators, "uses_history")


def test_an_unclear_case_is_judged_on_offering_concrete_options() -> None:
    for case in _cases("unclear"):
        judge = _judge(case.evaluators, "offers_options")
        assert "option" in judge.rubric.lower()


@pytest.mark.parametrize("word", ["acknowledge", "aggravate", "recommend", "professional"])
def test_every_pain_case_is_judged_on_the_g3_safety_rule(word: str) -> None:
    for case in _cases("pain"):
        judge = _judge(case.evaluators, "pain_safety")
        assert word in judge.rubric.lower(), case.name
        assert judge.include_input


_PAIN_WORDS = ("pain", "hurt", "ache", "aching", "injur", "dizzy", "twinge", "sore", "unwell")


def test_only_pain_cases_mention_pain_so_none_escapes_the_safety_judge() -> None:
    for case in _dataset().cases:
        if str(case.name).startswith("pain:"):
            continue
        message = case.inputs.prompt.lower()
        assert not any(word in message for word in _PAIN_WORDS), case.name


def test_every_reply_is_judged_on_no_diagnosis_and_tone() -> None:
    evaluators = _dataset().evaluators

    no_diagnosis = _judge(evaluators, "no_diagnosis")
    tone = _judge(evaluators, "tone")

    for word in ("diagnosis", "rule out", "hint"):
        assert word in no_diagnosis.rubric.lower()
    assert no_diagnosis.include_input
    assert tone.include_input


def test_no_judge_pins_its_own_model() -> None:
    """The judge model comes from `EVAL_JUDGE_MODEL` via `set_default_judge_model` (ADR-0009)."""
    dataset = _dataset()
    evaluators = [*dataset.evaluators, *(e for case in dataset.cases for e in case.evaluators)]

    judges = [evaluator for evaluator in evaluators if isinstance(evaluator, LLMJudge)]

    assert judges
    assert all(judge.model is None for judge in judges)


# JevCheck (#124): Jev answers binary rubric items next to the judge of record.


def _checks(evaluators: list[Evaluator[EvalCaseInputs, str, object]]) -> list[JevCheck]:
    return [evaluator for evaluator in evaluators if isinstance(evaluator, JevCheck)]


def _judge_names(evaluators: list[Evaluator[EvalCaseInputs, str, object]]) -> set[str]:
    return {
        str(evaluator.assertion["evaluation_name"])
        for evaluator in evaluators
        if isinstance(evaluator, LLMJudge) and isinstance(evaluator.assertion, dict)
    }


def test_every_reply_gets_a_jev_no_diagnosis_check_paired_with_the_judge() -> None:
    [check] = _checks(_dataset().evaluators)

    assert (check.decision_set, check.rubric) == ("chitchat_rubric", "no_diagnosis")
    assert (check.pairs_with or check.rubric) in _judge_names(_dataset().evaluators)


def test_pain_cases_check_pain_first_and_unclear_cases_check_for_no_free_text_question() -> None:
    for prefix, rubric, judge in [
        ("pain", "pain_first", "pain_safety"),
        ("unclear", "no_free_text_question", "offers_options"),
    ]:
        for case in _cases(prefix):
            [check] = _checks(case.evaluators)
            assert (check.rubric, check.pairs_with) == (rubric, judge), case.name


def test_every_check_names_a_question_of_its_set_and_a_judge_on_the_same_case() -> None:
    dataset = _dataset()
    for case in dataset.cases:
        judges = _judge_names(case.evaluators) | _judge_names(dataset.evaluators)
        for check in _checks(case.evaluators) + _checks(dataset.evaluators):
            output_type = (
                DecisionSetLoader()
                .load(check.decision_set, check.version, questions={check.rubric})
                .output_type
            )
            assert list(output_type.model_fields) == [check.rubric], case.name
            assert (check.pairs_with or check.rubric) in judges, case.name
            assert 0 < check.threshold < 1, case.name
