"""The committed router eval dataset covers the ticket's cases (ADR-0009, ticket #80)."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_evals import Dataset

from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.router import (
    IntentKindsMatch,
    LogSessionSportMatch,
    router_output_type,
)

_DATASET = Path(__file__).parents[2] / "evals" / "datasets" / "router.yaml"


def _dataset() -> Dataset[EvalCaseInputs, Any, object]:
    output_type = router_output_type(default_sport_registry())
    return Dataset[EvalCaseInputs, output_type, object].from_file(  # type: ignore[valid-type]
        _DATASET, custom_evaluator_types=(IntentKindsMatch, LogSessionSportMatch)
    )


def _kinds(expected: BaseModel | None) -> list[str]:
    assert expected is not None
    return [intent.kind for intent in expected.intents]  # type: ignore[attr-defined]


def _cases() -> dict[str, list[str]]:
    return {str(case.name): _kinds(case.expected_output) for case in _dataset().cases}


def test_the_dataset_covers_every_required_message_kind() -> None:
    kinds = list(_cases().values())

    assert ["chitchat"] in kinds
    assert ["log_session"] in kinds
    assert ["ask_training_question"] in kinds
    assert ["request_plan"] in kinds
    assert ["log_session", "ask_training_question"] in kinds


def test_a_pain_mention_is_ranked_first_even_next_to_a_log() -> None:
    kinds_per_case = list(_cases().values())
    mixed = [kinds for kinds in kinds_per_case if "wellbeing_or_injury" in kinds and len(kinds) > 1]

    assert ["wellbeing_or_injury"] in kinds_per_case
    assert ["wellbeing_or_injury", "log_session"] in kinds_per_case
    assert all(kinds[0] == "wellbeing_or_injury" for kinds in mixed)


def test_the_dataset_covers_sport_inference_cases() -> None:
    names = " ".join(_cases())

    assert "single-sport" in names
    assert "grades" in names
    assert "ambiguous" in names


def test_every_case_carries_its_message_in_the_deps() -> None:
    for case in _dataset().cases:
        assert case.inputs.deps["message"] == case.inputs.prompt, case.name


def test_the_ambiguous_two_sport_case_accepts_climbing_or_gym() -> None:
    case = next(c for c in _dataset().cases if c.name == "log ambiguous two sports")

    assert case.metadata == {"acceptable_sports": ["climbing", "gym"]}


def test_pain_cases_with_an_implied_session_accept_the_log_but_do_not_require_it() -> None:
    cases = {str(case.name): case for case in _dataset().cases}

    for name in ("feeling unwell", "injury wording in slang"):
        assert _kinds(cases[name].expected_output) == ["wellbeing_or_injury"], name
        assert cases[name].metadata == {"optional_trailing_kinds": ["log_session"]}, name


def test_the_prompt_injection_case_still_expects_chitchat() -> None:
    assert _cases()["prompt injection attempt"] == ["chitchat"]


def test_small_talk_carrying_a_pain_mention_expects_pain_before_chitchat() -> None:
    pain_then_chitchat = [
        name for name, kinds in _cases().items() if kinds == ["wellbeing_or_injury", "chitchat"]
    ]

    assert len(pain_then_chitchat) >= 3


def test_a_played_down_pain_mention_in_a_greeting_still_expects_pain_first() -> None:
    assert _cases()["played-down pain plus greeting"] == ["wellbeing_or_injury", "chitchat"]


def test_every_miss_pattern_of_the_first_baseline_has_paraphrases() -> None:
    cases = _cases()
    relative_date_logs = [name for name in cases if name.startswith("relative date log")]
    polish_cases = [name for name in cases if name.startswith("polish")]

    assert len(relative_date_logs) >= 4
    assert all(cases[name] == ["log_session"] for name in relative_date_logs)
    assert "polish relative date log" in cases
    assert cases["polish relative date log"] == ["log_session"]
    assert len(polish_cases) >= 6


def test_polish_messages_cover_a_log_a_profile_update_chitchat_and_pain() -> None:
    kinds = {tuple(_cases()[name]) for name in _cases() if name.startswith("polish")}

    assert {
        ("log_session",),
        ("update_profile",),
        ("chitchat",),
        ("wellbeing_or_injury",),
    } <= kinds
