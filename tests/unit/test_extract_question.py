"""`climbing.extract` v3 may return a question instead of a draft (B17, ADR-0015, ticket #164),
and so may `gym.extract` and `cycling.extract` v3 (ticket #206): their output, the recent context
and the athlete's answer in their deps, and the resumed run that can only draft."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_ai import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_evals.evaluators import EvaluatorContext

from ai_trainer.domain.sports.climbing.extraction import ClimbingExtraction, ClimbingQuestion
from ai_trainer.domain.sports.cycling.extraction import CyclingQuestion
from ai_trainer.domain.sports.gym.extraction import GymExtraction, GymQuestion
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.conversation import build_prompt_registry
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.extract import (
    ActivitiesMatch,
    DateMatch,
    ExtractDeps,
    QuestionMatch,
    build_extract_deps,
    extract_template,
)
from ai_trainer.llm.prompts.registry import PromptRegistry

_PROMPTS_ROOT = Path(__file__).parents[2] / "src" / "ai_trainer" / "llm" / "prompts"
_CLIMBING_EXTRACT = "climbing.extract"
_NOW = datetime(2026, 10, 2, 17, 0, tzinfo=UTC)
_QUESTION = {"readings": [{"kind": "boulder", "grade": "7a"}, {"kind": "lead", "grade": "7a"}]}


def _deps(**overrides: Any) -> ExtractDeps:
    values: dict[str, Any] = {
        "message": "Did 3 7a",
        "history": [],
        "now": _NOW,
        "timezone": "Europe/Warsaw",
        "history_turns": 10,
    }
    return build_extract_deps(**(values | overrides))


def _registry() -> PromptRegistry:
    return build_prompt_registry(_PROMPTS_ROOT, default_sport_registry())


def _climbing_spec() -> Any:
    spec = default_sport_registry().get("climbing").extraction
    assert spec is not None
    return spec


def test_deps_carry_the_recent_kinds_and_the_answers() -> None:
    deps = _deps(recent=("boulder", "lead"), answers=("boulder",))

    assert (deps.recent, deps.answers) == (["boulder", "lead"], ["boulder"])


def test_deps_default_to_no_recent_kinds_and_no_answers() -> None:
    deps = _deps()

    assert (deps.recent, deps.answers) == ([], [])


def test_the_asking_version_offers_the_draft_or_the_question() -> None:
    template = extract_template(_climbing_spec())

    assert template.version == 3
    assert template.output_type == (ClimbingExtraction, ClimbingQuestion)


def test_a_version_before_the_question_can_only_draft() -> None:
    assert extract_template(_climbing_spec(), version=2).output_type is ClimbingExtraction


def test_a_run_that_may_not_ask_can_only_draft() -> None:
    template = extract_template(_climbing_spec(), may_ask=False)

    assert (template.version, template.output_type) == (3, ClimbingExtraction)


def test_a_sport_with_no_question_can_only_draft() -> None:
    spec = _climbing_spec().model_copy(update={"question": None})

    assert extract_template(spec).output_type is spec.output_type


def test_gym_and_cycling_offer_the_draft_or_their_question_from_v3() -> None:
    """Ticket #206; the active version keeps it (gym v4, ticket #373)."""
    for sport_id, question in (("gym", GymQuestion), ("cycling", CyclingQuestion)):
        spec = default_sport_registry().get(sport_id).extraction
        assert spec is not None

        assert extract_template(spec, version=3).output_type == (spec.output_type, question)
        assert extract_template(spec).output_type == (spec.output_type, question)
        assert extract_template(spec, version=2).output_type is spec.output_type


def _answering(tool: str, args: dict[str, Any], seen: list[str] | None = None) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if seen is not None:
            seen.append(str(messages[0].instructions))  # type: ignore[union-attr]
        [picked] = [output.name for output in info.output_tools if tool in output.name]
        return ModelResponse(parts=[ToolCallPart(picked, args)])

    return FunctionModel(respond)


async def test_the_v3_agent_returns_a_question_with_its_readings_likeliest_first() -> None:
    agent = _registry().build_agent(_CLIMBING_EXTRACT, 3)

    result = await agent.run(
        "Did 3 7a",
        deps=_deps(recent=("boulder", "lead")),
        model=_answering("ClimbingQuestion", _QUESTION),
    )

    assert result.output == ClimbingQuestion.model_validate(_QUESTION)
    assert [option.id for option in result.output.options()] == ["boulder", "lead"]


async def test_the_v3_agent_still_returns_a_draft() -> None:
    draft = {
        "occurred_on": "2026-10-02",
        "date_source": "assumed_today",
        "activities": [{"kind": "lead", "count": 3, "grade": "7a"}],
    }
    agent = _registry().build_agent(_CLIMBING_EXTRACT, 3)

    result = await agent.run(
        "Did 3 7a", deps=_deps(recent=("lead",)), model=_answering("ClimbingExtraction", draft)
    )

    assert result.output == ClimbingExtraction.model_validate(draft)


async def test_the_v3_prompt_carries_the_recent_kinds_and_no_answer_before_one() -> None:
    seen: list[str] = []
    agent = _registry().build_agent(_CLIMBING_EXTRACT, 3)

    await agent.run(
        "Did 3 7a",
        deps=_deps(recent=("boulder", "lead")),
        model=_answering("ClimbingQuestion", _QUESTION, seen),
    )

    assert "recent sessions, most recent first: boulder, lead." in seen[0]
    assert "has answered" not in seen[0]
    assert seen[0].rstrip().endswith("Did 3 7a")


def test_the_v3_prompt_says_none_yet_with_no_recent_sessions() -> None:
    rendered = _registry().render_instructions(_CLIMBING_EXTRACT, 3, _deps())

    assert "recent sessions, most recent first: none yet." in rendered


def test_the_v3_prompt_carries_the_athletes_answer() -> None:
    rendered = _registry().render_instructions(
        _CLIMBING_EXTRACT, 3, _deps(recent=("boulder", "lead"), answers=("boulder",))
    )

    assert "climbs this log left open were: boulder. Draft the log with that kind" in rendered


def _ctx(output: BaseModel, expected: BaseModel) -> EvaluatorContext[EvalCaseInputs, BaseModel]:
    return EvaluatorContext(
        name="case",
        inputs=EvalCaseInputs(prompt="Did 3 7a"),
        metadata=None,
        expected_output=expected,
        output=output,
        duration=0.0,
        _span_tree=None,  # type: ignore[arg-type]
        attributes={},
        metrics={},
    )


_A_DRAFT = ClimbingExtraction.model_validate(
    {
        "occurred_on": "2026-10-02",
        "date_source": "assumed_today",
        "activities": [{"kind": "lead", "count": 3, "grade": "7a"}],
    }
)


def test_question_match_passes_a_question_with_the_same_readings_in_any_order() -> None:
    expected = ClimbingQuestion.model_validate(_QUESTION)
    reordered = ClimbingQuestion.model_validate({"readings": list(reversed(_QUESTION["readings"]))})

    assert QuestionMatch().evaluate(_ctx(reordered, expected)) is True


def test_question_match_fails_a_draft_where_a_question_was_due_and_the_reverse() -> None:
    question = ClimbingQuestion.model_validate(_QUESTION)

    assert QuestionMatch().evaluate(_ctx(_A_DRAFT, question)) is False
    assert QuestionMatch().evaluate(_ctx(question, _A_DRAFT)) is False


def test_question_match_fails_other_readings() -> None:
    other = ClimbingQuestion.model_validate(
        {"readings": [{"kind": "boulder", "grade": "7a"}, {"kind": "top_rope", "grade": "7a"}]}
    )

    assert (
        QuestionMatch().evaluate(_ctx(other, ClimbingQuestion.model_validate(_QUESTION))) is False
    )


def test_question_match_passes_a_draft_where_a_draft_was_due() -> None:
    assert QuestionMatch().evaluate(_ctx(_A_DRAFT, _A_DRAFT)) is True


def test_draft_graders_fail_a_question_where_a_draft_was_due() -> None:
    question = ClimbingQuestion.model_validate(_QUESTION)

    assert DateMatch().evaluate(_ctx(question, _A_DRAFT)) is False
    assert ActivitiesMatch().evaluate(_ctx(question, _A_DRAFT)) is False


def test_draft_graders_grade_nothing_where_a_question_was_due() -> None:
    """`QuestionMatch` grades that case; the date and activities have nothing to compare."""
    question = ClimbingQuestion.model_validate(_QUESTION)

    assert DateMatch().evaluate(_ctx(_A_DRAFT, question)) == {}
    assert ActivitiesMatch().evaluate(_ctx(question, question)) == {}


def test_an_answered_run_of_the_asking_template_can_only_draft() -> None:
    """Evals run each case with the output the app would offer it (ticket #164)."""
    template = extract_template(_climbing_spec())
    assert template.output_for is not None

    assert template.output_for(_deps(answers=("boulder",))) is ClimbingExtraction
    assert template.output_for(_deps()) == (ClimbingExtraction, ClimbingQuestion)


def test_an_answered_run_is_narrowed_only_for_extraction_deps() -> None:
    template = extract_template(_climbing_spec())
    assert template.output_for is not None

    assert template.output_for(EvalCaseInputs(prompt="x")) == (
        ClimbingExtraction,
        ClimbingQuestion,
    )


def test_a_template_that_never_asks_needs_no_narrowing() -> None:
    assert extract_template(_climbing_spec(), version=2).output_for is None
    assert extract_template(_climbing_spec(), may_ask=False).output_for is None


_PRESS = {"about": "movement", "exercise": "press", "readings": ["overhead press", "bench press"]}


async def test_the_gym_v3_agent_asks_which_movement() -> None:
    agent = _registry().build_agent("gym.extract", 3)

    result = await agent.run(
        "Did 5x5 press at 60",
        deps=_deps(message="Did 5x5 press at 60"),
        model=_answering("GymQuestion", _PRESS),
    )

    assert result.output == GymQuestion.model_validate(_PRESS)


async def test_the_cycling_v3_agent_asks_km_or_miles() -> None:
    question = {"distance": "40", "readings": ["mi", "km"]}
    agent = _registry().build_agent("cycling.extract", 3)

    result = await agent.run(
        "Rode 40",
        deps=_deps(message="Rode 40", recent=("mi", "km")),
        model=_answering("CyclingQuestion", question),
    )

    assert result.output == CyclingQuestion.model_validate(question)


def test_the_gym_v3_prompt_carries_the_recent_exercises_and_the_answer() -> None:
    rendered = _registry().render_instructions(
        "gym.extract",
        3,
        _deps(recent=("bench press (kg)", "pull-ups"), answers=("overhead press",)),
    )

    assert "recent sessions, most recent first: bench press (kg), pull-ups." in rendered
    assert "The athlete has answered what the log left open: overhead press." in rendered


def test_the_cycling_v3_prompt_carries_the_recent_units_and_none_yet() -> None:
    registry = _registry()

    assert "recent rides, most recent first: mi, km." in registry.render_instructions(
        "cycling.extract", 3, _deps(recent=("mi", "km"))
    )
    rendered = registry.render_instructions("cycling.extract", 3, _deps(answers=("km",)))
    assert "recent rides, most recent first: none yet." in rendered
    assert "The athlete has answered the unit of the distance this log left open: km." in rendered


def test_question_match_compares_a_gym_question_whole_with_its_readings_in_any_order() -> None:
    expected = GymQuestion.model_validate(_PRESS)
    reordered = GymQuestion.model_validate(_PRESS | {"readings": ["bench press", "overhead press"]})
    other_exercise = GymQuestion.model_validate(_PRESS | {"exercise": "row"})
    a_draft = GymExtraction.model_validate(
        {
            "occurred_on": "2026-10-02",
            "date_source": "assumed_today",
            "activities": [{"name": "overhead press"}],
        }
    )

    assert QuestionMatch().evaluate(_ctx(reordered, expected)) is True
    assert QuestionMatch().evaluate(_ctx(other_exercise, expected)) is False
    assert QuestionMatch().evaluate(_ctx(a_draft, expected)) is False
