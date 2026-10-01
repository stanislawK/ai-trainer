"""Code-graded router evaluators (ADR-0009, ticket #80)."""

from typing import Any

from pydantic import BaseModel
from pydantic_evals.evaluators import EvaluatorContext

from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.router import IntentKindsMatch, LogSessionSportMatch, router_output_type


def _output(*intents: dict[str, Any]) -> BaseModel:
    return router_output_type(default_sport_registry()).model_validate({"intents": list(intents)})


def _intent(kind: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "confidence": 0.9, "span": "x", **extra}


def _ctx(output: BaseModel, expected: BaseModel) -> EvaluatorContext[EvalCaseInputs, BaseModel]:
    return EvaluatorContext(
        name="case",
        inputs=EvalCaseInputs(prompt="x"),
        metadata=None,
        expected_output=expected,
        output=output,
        duration=0.0,
        _span_tree=None,  # type: ignore[arg-type]
        attributes={},
        metrics={},
    )


def test_kinds_match_is_true_only_for_the_same_kinds_in_the_same_order() -> None:
    expected = _output(_intent("wellbeing_or_injury"), _intent("chitchat"))

    same = _output(_intent("wellbeing_or_injury"), _intent("chitchat"))
    swapped = _output(_intent("chitchat"), _intent("wellbeing_or_injury"))

    assert IntentKindsMatch().evaluate(_ctx(same, expected)) is True
    assert IntentKindsMatch().evaluate(_ctx(swapped, expected)) is False


def test_sport_match_grades_each_expected_log_session_sport() -> None:
    expected = _output(
        _intent("log_session", sport="gym"), _intent("log_session", sport="climbing")
    )

    right = _output(_intent("log_session", sport="gym"), _intent("log_session", sport="climbing"))
    wrong = _output(_intent("log_session", sport="gym"), _intent("log_session", sport="gym"))

    assert LogSessionSportMatch().evaluate(_ctx(right, expected)) == 1.0
    assert LogSessionSportMatch().evaluate(_ctx(wrong, expected)) == 0.5


def test_sport_match_is_full_marks_when_no_log_session_is_expected() -> None:
    expected = _output(_intent("chitchat"))

    assert LogSessionSportMatch().evaluate(_ctx(_output(_intent("chitchat")), expected)) == 1.0
