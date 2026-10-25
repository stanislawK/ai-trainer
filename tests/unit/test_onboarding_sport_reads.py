"""`onboarding.goals`, `.level` and `.schedule` and their reads (PRD 0008 B33, B34, B15,
ADR-0028, ticket #255): each step's prose is read with its own template and the onboarding
settings group, dates and lengths come back as parts and code works them out, and the evals grade
the worked-out values."""

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel, PostgresDsn, SecretStr
from pydantic_ai import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_evals.evaluators import EvaluatorContext

from ai_trainer.application.ports.llm_gateway import LlmGatewayResult
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.onboarding import AnswerStatus, OnboardingAnswer, OnboardingStep
from ai_trainer.domain.sport_profile import LengthUnit, Weekday
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.conversation import build_prompt_registry
from ai_trainer.llm.evals.inputs import EvalCaseInputs
from ai_trainer.llm.gateway import OpenRouterGateway
from ai_trainer.llm.onboarding import (
    GOALS_TEMPLATE_ID,
    LEVEL_TEMPLATE_ID,
    SCHEDULE_TEMPLATE_ID,
    GoalsMatch,
    GoalsRead,
    LevelMatch,
    LevelRead,
    LlmOnboardingReader,
    OnboardingDeps,
    ReadGoal,
    ReadLength,
    ReadTargetDate,
    ScheduleMatch,
    ScheduleRead,
    onboarding_templates,
)
from ai_trainer.settings import Settings
from tests.unit.test_llm_gateway import FakeLlmCallsRepository

_PROMPTS_ROOT = Path(__file__).parents[2] / "src" / "ai_trainer" / "llm" / "prompts"
# An October evening in Warsaw (ADR-0014).
_NOW = datetime(2026, 10, 25, 18, 0, tzinfo=UTC)


class FakeClock:
    def now(self) -> datetime:
        return _NOW


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url=PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        openrouter_api_key=SecretStr("test-key"),
        eval_judge_model="test/judge-model",
    )


def _answer(step: OnboardingStep, text: str) -> OnboardingAnswer:
    return OnboardingAnswer(
        step=step, sport_id="climbing", text=text, revision=1, status=AnswerStatus.PENDING
    )


def _replying(read: Mapping[str, object]) -> FunctionModel:
    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(json.dumps(read))], provider_details={"cost": 0.0001})

    return FunctionModel(reply)


def _reader(gateway: object) -> LlmOnboardingReader:
    return LlmOnboardingReader(
        registry=build_prompt_registry(_PROMPTS_ROOT, default_sport_registry()),
        gateway=gateway,  # type: ignore[arg-type]
        clock=FakeClock(),
        model="test/onboarding-model",
    )


async def test_by_june_sent_in_october_reads_as_june_next_year() -> None:
    """AC: "Send my first 7c outdoors by June", sent in October from `Europe/Warsaw`, gets a
    target date in June of the next year."""
    gateway = OpenRouterGateway(_settings(), FakeLlmCallsRepository())
    # The model names only the month the athlete did; code picks next June (invariant 6).
    read = {"goals": [{"text": "Send my first 7c outdoors", "target": {"month": 6}}]}
    with gateway.agent.override(model=_replying(read)):
        parsed = await _reader(gateway).read(
            uuid4(),
            _answer(OnboardingStep.GOALS, "Send my first 7c outdoors by June"),
            sport_id="climbing",
            timezone="Europe/Warsaw",
        )

    assert parsed == {"goals": [{"text": "Send my first 7c outdoors", "target_date": "2027-06-30"}]}


async def test_the_schedule_ac_answer_reads_as_4_sessions_of_120_minutes_on_four_days() -> None:
    """AC: "4 times a week, around 2 hours, Mon Wed Fri Sat" shows 4, 120 min and Mon, Wed, Fri,
    Sat. The hours become minutes in code (ADR-0028 invariant 6)."""
    gateway = OpenRouterGateway(_settings(), FakeLlmCallsRepository())
    read = {
        "sessions_per_week": 4,
        "session_length": {"value": 2, "unit": "h"},
        "days": ["mon", "wed", "fri", "sat"],
    }
    with gateway.agent.override(model=_replying(read)):
        parsed = await _reader(gateway).read(
            uuid4(),
            _answer(OnboardingStep.SCHEDULE, "4 times a week, around 2 hours, Mon Wed Fri Sat"),
            sport_id="climbing",
            timezone="Europe/Warsaw",
        )

    assert parsed == {
        "sessions_per_week": 4,
        "session_minutes": 120,
        "preferred_days": [0, 2, 4, 5],
    }


async def test_the_level_ac_answer_puts_the_lift_under_level_and_the_split_under_exercises() -> (
    None
):
    """AC: "Deadlift 125 kg, push pull legs" puts the lift under level and the split under
    exercises."""
    gateway = OpenRouterGateway(_settings(), FakeLlmCallsRepository())
    read = {"level": "Deadlift 125 kg", "exercises": "Push pull legs"}
    with gateway.agent.override(model=_replying(read)):
        parsed = await _reader(gateway).read(
            uuid4(),
            _answer(OnboardingStep.LEVEL, "Deadlift 125 kg, push pull legs"),
            sport_id="gym",
            timezone="Europe/Warsaw",
        )

    assert parsed == {"level": "Deadlift 125 kg", "exercises": "Push pull legs"}


@pytest.mark.parametrize(
    ("step", "template_id", "output"),
    [
        (OnboardingStep.GOALS, GOALS_TEMPLATE_ID, GoalsRead()),
        (OnboardingStep.LEVEL, LEVEL_TEMPLATE_ID, LevelRead()),
        (OnboardingStep.SCHEDULE, SCHEDULE_TEMPLATE_ID, ScheduleRead()),
    ],
)
async def test_each_step_reads_with_its_own_template_and_the_athletes_day(
    step: OnboardingStep, template_id: str, output: BaseModel
) -> None:
    sent: dict[str, object] = {}

    class RecordingGateway:
        async def run(self, **call: object) -> object:
            sent.update(call)
            return LlmGatewayResult(
                output=output, friendly_error=None, outcome=LlmCallOutcome.SUCCESS
            )

    parsed = await _reader(RecordingGateway()).read(
        uuid4(), _answer(step, "Whatever"), sport_id="cycling", timezone="Europe/Warsaw"
    )

    assert parsed is not None
    assert sent["template_id"] == template_id
    assert sent["template_version"] == 1
    assert sent["prompt"] == "Whatever"
    assert sent["temperature"] == 0
    instructions = sent["instructions"]
    assert isinstance(instructions, str)
    assert "cycling" in instructions
    assert "2026-10-25" in instructions
    assert "Europe/Warsaw" in instructions


def test_the_sport_templates_share_the_onboarding_settings_group() -> None:
    templates = {template.id: template for template in onboarding_templates()}

    for template_id in (GOALS_TEMPLATE_ID, LEVEL_TEMPLATE_ID, SCHEDULE_TEMPLATE_ID):
        template = templates[template_id]
        assert template.model_settings_key == "onboarding_model"
        assert template.provider_settings_key == "onboarding_provider_preferences"
        assert template.reasoning_effort_settings_key == "onboarding_reasoning_effort"
        assert template.persona is False


def test_each_template_renders_its_deps() -> None:
    registry = build_prompt_registry(_PROMPTS_ROOT, default_sport_registry())
    deps = OnboardingDeps(sport="gym", today=date(2026, 10, 25), timezone="Europe/Warsaw")

    for template_id in (GOALS_TEMPLATE_ID, LEVEL_TEMPLATE_ID, SCHEDULE_TEMPLATE_ID):
        rendered = registry.render_instructions(template_id, 1, deps)
        assert "gym" in rendered
        assert "2026-10-25" in rendered


def test_a_goal_without_a_target_has_no_date_and_an_impossible_one_stays_empty() -> None:
    read = GoalsRead(
        goals=[
            ReadGoal(text="Climb more"),
            ReadGoal(text="Ride 160 km", target=ReadTargetDate(year=2027, month=2, day=30)),
        ]
    )

    assert read.converted(today=date(2026, 10, 25)) == {
        "goals": [
            {"text": "Climb more", "target_date": None},
            {"text": "Ride 160 km", "target_date": None},
        ]
    }


def test_a_schedule_out_of_bounds_stays_empty() -> None:
    read = ScheduleRead(
        sessions_per_week=20,
        session_length=ReadLength(value=12, unit=LengthUnit.H),
        days=[Weekday.SUN, Weekday.MON, Weekday.MON],
    )

    assert read.converted() == {
        "sessions_per_week": None,
        "session_minutes": None,
        "preferred_days": [0, 6],
    }


def _ctx(output: BaseModel, expected: BaseModel) -> EvaluatorContext[EvalCaseInputs, object]:
    return EvaluatorContext(
        name="case",
        inputs=EvalCaseInputs(
            prompt="x", deps={"sport": "climbing", "today": "2026-10-25", "timezone": "UTC"}
        ),
        metadata=None,
        expected_output=expected,
        output=output,
        duration=0.0,
        _span_tree=None,  # type: ignore[arg-type]
        attributes={},
        metrics={},
    )


def test_the_goals_eval_grades_the_worked_out_dates() -> None:
    """A month alone and next year's last day of it name the same date, on the case's own day."""
    expected = GoalsRead(goals=[ReadGoal(text="First 7c", target=ReadTargetDate(month=6))])
    output = GoalsRead(
        goals=[ReadGoal(text="Send 7c", target=ReadTargetDate(year=2027, month=6, day=30))]
    )

    assert GoalsMatch().evaluate(_ctx(output, expected)) == {  # type: ignore[arg-type]
        "goal_count": True,
        "target_dates": True,
    }


def test_the_goals_eval_fails_a_wrong_year_or_an_extra_goal() -> None:
    expected = GoalsRead(goals=[ReadGoal(text="x", target=ReadTargetDate(year=2027, month=6))])
    output = GoalsRead(
        goals=[ReadGoal(text="x", target=ReadTargetDate(year=2026, month=6)), ReadGoal(text="y")]
    )

    assert GoalsMatch().evaluate(_ctx(output, expected)) == {  # type: ignore[arg-type]
        "goal_count": False,
        "target_dates": False,
    }


def test_the_level_eval_grades_which_fields_are_filled() -> None:
    expected = LevelRead(level="Deadlift 125 kg", exercises="Push pull legs")
    output = LevelRead(level="Deadlift 125 kg, push pull legs")

    assert LevelMatch().evaluate(_ctx(output, expected)) == {  # type: ignore[arg-type]
        "level_present": True,
        "exercises_present": False,
    }


def test_the_schedule_eval_grades_the_worked_out_values() -> None:
    expected = ScheduleRead(
        sessions_per_week=4,
        session_length=ReadLength(value=2, unit=LengthUnit.H),
        days=[Weekday.MON, Weekday.WED],
    )
    output = ScheduleRead(
        sessions_per_week=4,
        session_length=ReadLength(value=120, unit=LengthUnit.MIN),
        days=[Weekday.WED],
    )

    assert ScheduleMatch().evaluate(_ctx(output, expected)) == {  # type: ignore[arg-type]
        "sessions_per_week": True,
        "session_minutes": True,
        "preferred_days": False,
    }


def test_a_blank_goal_is_dropped() -> None:
    read = GoalsRead(goals=[ReadGoal(text="  "), ReadGoal(text="Ride 100 km")])
    assert read.converted(today=date(2026, 10, 25)) == {
        "goals": [{"text": "Ride 100 km", "target_date": None}]
    }


async def test_a_month_alone_is_resolved_on_the_athletes_own_day() -> None:
    """At 23:30 UTC on 31 December it is already 1 January 2027 in Warsaw, so "by December" is
    December 2027 there, not the December that is ending (B15, ADR-0014)."""

    class NewYearsEve:
        def now(self) -> datetime:
            return datetime(2026, 12, 31, 23, 30, tzinfo=UTC)

    gateway = OpenRouterGateway(_settings(), FakeLlmCallsRepository())
    read = {"goals": [{"text": "Ride 160 km", "target": {"month": 12}}]}
    reader = LlmOnboardingReader(
        registry=build_prompt_registry(_PROMPTS_ROOT, default_sport_registry()),
        gateway=gateway,
        clock=NewYearsEve(),
        model="test/onboarding-model",
    )
    with gateway.agent.override(model=_replying(read)):
        parsed = await reader.read(
            uuid4(),
            _answer(OnboardingStep.GOALS, "Ride 160 km by December"),
            sport_id="cycling",
            timezone="Europe/Warsaw",
        )

    assert parsed == {"goals": [{"text": "Ride 160 km", "target_date": "2027-12-31"}]}
