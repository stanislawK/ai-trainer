"""The onboarding read templates (`onboarding.<step>`, ADR-0028): each step's prose becomes its
fields in the background. Values come back with the unit the athlete used, and dates as their
parts, and code converts them (invariant 6). A detail the answer leaves unclear stays empty
(B34)."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, JsonValue
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, EvaluatorOutput

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.llm_gateway import LlmGatewayPort
from ai_trainer.domain.athlete_profile import (
    AboutYou,
    HeightUnit,
    WeightUnit,
    bounded_age,
    bounded_limits,
    height_cm,
    weight_kg,
)
from ai_trainer.domain.llm_calls import ProviderPreferences, ReasoningEffort
from ai_trainer.domain.onboarding import OnboardingAnswer, OnboardingStep
from ai_trainer.domain.sport_profile import (
    MAX_EXERCISES_LENGTH,
    MAX_GOAL_LENGTH,
    MAX_LEVEL_LENGTH,
    LengthUnit,
    Weekday,
    bounded_sessions,
    bounded_text,
    session_minutes,
    target_date,
)
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.prompts.template import PromptTemplate

ABOUT_YOU_TEMPLATE_ID = "onboarding.about_you"
ABOUT_YOU_VERSION = 1
GOALS_TEMPLATE_ID = "onboarding.goals"
LEVEL_TEMPLATE_ID = "onboarding.level"
SCHEDULE_TEMPLATE_ID = "onboarding.schedule"
SPORT_STEP_VERSION = 1


class OnboardingDeps(BaseModel):
    """Every onboarding template's variables (ADR-0028): the sport the athlete is onboarding
    with, by its `SportRegistry` ID, and the athlete's local day and zone (ADR-0014)."""

    sport: str
    today: date
    timezone: str


class ReadHeight(BaseModel):
    value: float = Field(gt=0)
    unit: HeightUnit
    # The rest of a height in feet and inches; the feet are `value`.
    inches: float | None = Field(default=None, ge=0, lt=12)


class ReadWeight(BaseModel):
    value: float = Field(gt=0)
    unit: WeightUnit


class AboutYouRead(BaseModel):
    """What `onboarding.about_you` returns. Every field is optional; the bounds are checked in
    code, so an implausible value is dropped rather than retried."""

    age: int | None = Field(default=None, ge=1)
    height: ReadHeight | None = None
    weight: ReadWeight | None = None
    limits: str | None = None

    def converted(self) -> AboutYou:
        """In metric, within the profile's bounds (ADR-0028 invariant 6)."""
        return AboutYou(
            age=bounded_age(self.age) if self.age is not None else None,
            height_cm=height_cm(self.height.value, self.height.unit, inches=self.height.inches)
            if self.height is not None
            else None,
            weight_kg=weight_kg(self.weight.value, self.weight.unit)
            if self.weight is not None
            else None,
            limits=bounded_limits(self.limits) if self.limits is not None else None,
        )


class ReadTargetDate(BaseModel):
    """A goal's target date as the parts the athlete named; code picks the year a month alone
    falls in and the day a month or a year alone means (ADR-0028 invariant 6)."""

    year: int | None = None
    month: int | None = Field(default=None, ge=1, le=12)
    day: int | None = Field(default=None, ge=1, le=31)


class ReadGoal(BaseModel):
    text: str
    target: ReadTargetDate | None = None


class GoalsRead(BaseModel):
    """What `onboarding.goals` returns: each goal, with a target date when the answer names one."""

    goals: list[ReadGoal] = []

    def converted(self, *, today: date) -> dict[str, JsonValue]:
        """Each goal with its target date worked out on the athlete's own `today`."""
        goals: list[JsonValue] = []
        for goal in self.goals:
            if (text := bounded_text(goal.text, MAX_GOAL_LENGTH)) is None:
                continue
            when = (
                target_date(goal.target.year, goal.target.month, goal.target.day, today=today)
                if goal.target is not None
                else None
            )
            goals.append({"text": text, "target_date": when.isoformat() if when else None})
        return {"goals": goals}


class LevelRead(BaseModel):
    """What `onboarding.level` returns: where the athlete is now, and what they train."""

    level: str | None = None
    exercises: str | None = None

    def converted(self) -> dict[str, JsonValue]:
        return {
            "level": bounded_text(self.level, MAX_LEVEL_LENGTH) if self.level else None,
            "exercises": bounded_text(self.exercises, MAX_EXERCISES_LENGTH)
            if self.exercises
            else None,
        }


def _weekday_number(day: Weekday) -> int:
    return day.number


class ReadLength(BaseModel):
    value: float = Field(gt=0)
    unit: LengthUnit


class ScheduleRead(BaseModel):
    """What `onboarding.schedule` returns; the bounds are checked in code."""

    sessions_per_week: int | None = Field(default=None, ge=1)
    session_length: ReadLength | None = None
    days: list[Weekday] = []

    def converted(self) -> dict[str, JsonValue]:
        """In whole minutes and weekday numbers, within the profile's bounds (invariant 6)."""
        return {
            "sessions_per_week": bounded_sessions(self.sessions_per_week)
            if self.sessions_per_week is not None
            else None,
            "session_minutes": session_minutes(self.session_length.value, self.session_length.unit)
            if self.session_length is not None
            else None,
            "preferred_days": [day.number for day in sorted(set(self.days), key=_weekday_number)],
        }


def _onboarding_template[OutputT: BaseModel](
    template_id: str, version: int, output_type: type[OutputT]
) -> PromptTemplate[OnboardingDeps, OutputT]:
    return PromptTemplate(
        id=template_id,
        version=version,
        locale="en",
        deps_type=OnboardingDeps,
        output_type=output_type,
        # One settings group for every onboarding template (ADR-0028).
        model_settings_key="onboarding_model",
        provider_settings_key="onboarding_provider_preferences",
        reasoning_effort_settings_key="onboarding_reasoning_effort",
        # The app reads the output: the same answer should read the same way twice.
        temperature=0,
        output_retries=2,
        # It never writes to the athlete.
        persona=False,
    )


def about_you_template() -> PromptTemplate[OnboardingDeps, AboutYouRead]:
    return _onboarding_template(ABOUT_YOU_TEMPLATE_ID, ABOUT_YOU_VERSION, AboutYouRead)


def goals_template() -> PromptTemplate[OnboardingDeps, GoalsRead]:
    return _onboarding_template(GOALS_TEMPLATE_ID, SPORT_STEP_VERSION, GoalsRead)


def level_template() -> PromptTemplate[OnboardingDeps, LevelRead]:
    return _onboarding_template(LEVEL_TEMPLATE_ID, SPORT_STEP_VERSION, LevelRead)


def schedule_template() -> PromptTemplate[OnboardingDeps, ScheduleRead]:
    return _onboarding_template(SCHEDULE_TEMPLATE_ID, SPORT_STEP_VERSION, ScheduleRead)


def onboarding_templates() -> list[PromptTemplate[OnboardingDeps, BaseModel]]:
    return [about_you_template(), goals_template(), level_template(), schedule_template()]


@dataclass(frozen=True)
class _StepReader:
    template: PromptTemplate[OnboardingDeps, BaseModel]
    # The output's fields as stored, worked out on the athlete's own day.
    fields: Callable[[BaseModel, date], dict[str, JsonValue]]


def _about_you_fields(output: BaseModel, today: date) -> dict[str, JsonValue]:
    assert isinstance(output, AboutYouRead)
    return output.converted().model_dump(mode="json")


def _goals_fields(output: BaseModel, today: date) -> dict[str, JsonValue]:
    assert isinstance(output, GoalsRead)
    return output.converted(today=today)


def _converted_fields(output: BaseModel, today: date) -> dict[str, JsonValue]:
    assert isinstance(output, LevelRead | ScheduleRead)
    return output.converted()


_STEPS: dict[OnboardingStep, _StepReader] = {
    OnboardingStep.ABOUT_YOU: _StepReader(about_you_template(), _about_you_fields),
    OnboardingStep.GOALS: _StepReader(goals_template(), _goals_fields),
    OnboardingStep.LEVEL: _StepReader(level_template(), _converted_fields),
    OnboardingStep.SCHEDULE: _StepReader(schedule_template(), _converted_fields),
}


class LlmOnboardingReader:
    """Implements `OnboardingReaderPort` on the prompt registry and the gateway, with the one
    `onboarding_*` settings group (ADR-0028). Each read writes its `llm_calls` row."""

    def __init__(
        self,
        *,
        registry: PromptRegistry,
        gateway: LlmGatewayPort,
        clock: ClockPort,
        model: str,
        provider: ProviderPreferences | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        self._clock = clock
        self._model = model
        self._provider = provider
        self._reasoning_effort = reasoning_effort
        self._timeout_seconds = timeout_seconds

    async def read(
        self, user_id: UUID, answer: OnboardingAnswer, *, sport_id: str, timezone: str
    ) -> dict[str, JsonValue] | None:
        step = _STEPS[answer.step]
        template = step.template
        deps = OnboardingDeps(
            sport=sport_id,
            today=self._clock.now().astimezone(ZoneInfo(timezone)).date(),
            timezone=timezone,
        )
        result = await self._gateway.run(
            user_id=user_id,
            template_id=template.id,
            template_version=template.version,
            model_id=self._model,
            output_type=template.output_type,
            instructions=self._registry.render_instructions(template.id, template.version, deps),
            prompt=answer.text or "",
            temperature=template.temperature,
            output_retries=template.output_retries,
            provider=self._provider,
            reasoning_effort=self._reasoning_effort,
            timeout_seconds=self._timeout_seconds,
        )
        if result.output is None:
            return None
        return step.fields(result.output, deps.today)


@dataclass
class AboutYouMatch(Evaluator[EvalCaseInputs, BaseModel]):
    """Code-graded (B34): age, height and weight exactly as expected after conversion, and
    limits there exactly when expected; what the limits say is the judge's to grade."""

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> EvaluatorOutput:
        assert isinstance(ctx.output, AboutYouRead)
        assert isinstance(ctx.expected_output, AboutYouRead)
        got, want = ctx.output.converted(), ctx.expected_output.converted()
        return {
            "age": got.age == want.age,
            "height_cm": got.height_cm == want.height_cm,
            "weight_kg": got.weight_kg == want.weight_kg,
            "limits_present": (got.limits is None) == (want.limits is None),
        }


def _target_dates(goals: list[JsonValue]) -> list[JsonValue]:
    return [goal.get("target_date") for goal in goals if isinstance(goal, dict)]


@dataclass
class GoalsMatch(Evaluator[EvalCaseInputs, BaseModel]):
    """Code-graded (B34, B15): as many goals as expected, with the same target dates worked out
    on the case's own day, in order; what each goal says is the judge's to grade."""

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> EvaluatorOutput:
        assert isinstance(ctx.output, GoalsRead)
        assert isinstance(ctx.expected_output, GoalsRead)
        today = OnboardingDeps.model_validate(ctx.inputs.deps).today
        got = ctx.output.converted(today=today)["goals"]
        want = ctx.expected_output.converted(today=today)["goals"]
        assert isinstance(got, list)
        assert isinstance(want, list)
        return {
            "goal_count": len(got) == len(want),
            "target_dates": _target_dates(got) == _target_dates(want),
        }


@dataclass
class LevelMatch(Evaluator[EvalCaseInputs, BaseModel]):
    """Code-graded: level and exercises are there exactly when expected; what goes in each is
    the judge's to grade."""

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> EvaluatorOutput:
        assert isinstance(ctx.output, LevelRead)
        assert isinstance(ctx.expected_output, LevelRead)
        got, want = ctx.output.converted(), ctx.expected_output.converted()
        return {
            "level_present": (got["level"] is None) == (want["level"] is None),
            "exercises_present": (got["exercises"] is None) == (want["exercises"] is None),
        }


@dataclass
class ScheduleMatch(Evaluator[EvalCaseInputs, BaseModel]):
    """Code-graded: sessions, minutes and days exactly as expected after conversion, so "2 h"
    and "120 min" both pass."""

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> EvaluatorOutput:
        assert isinstance(ctx.output, ScheduleRead)
        assert isinstance(ctx.expected_output, ScheduleRead)
        got, want = ctx.output.converted(), ctx.expected_output.converted()
        return {field: got[field] == want[field] for field in want}
