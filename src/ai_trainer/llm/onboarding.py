"""The onboarding read templates (`onboarding.<step>`, ADR-0028): each step's prose becomes its
fields in the background. Values come back with the unit the athlete used, and code converts
them (invariant 6). A detail the answer leaves unclear stays empty (B34)."""

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
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.prompts.template import PromptTemplate

ABOUT_YOU_TEMPLATE_ID = "onboarding.about_you"
ABOUT_YOU_VERSION = 1


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


def about_you_template() -> PromptTemplate[OnboardingDeps, AboutYouRead]:
    return PromptTemplate(
        id=ABOUT_YOU_TEMPLATE_ID,
        version=ABOUT_YOU_VERSION,
        locale="en",
        deps_type=OnboardingDeps,
        output_type=AboutYouRead,
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


def onboarding_templates() -> list[PromptTemplate[OnboardingDeps, BaseModel]]:
    return [about_you_template()]


@dataclass(frozen=True)
class _StepReader:
    template: PromptTemplate[OnboardingDeps, BaseModel]
    fields: Callable[[BaseModel], dict[str, JsonValue]]


def _about_you_fields(output: BaseModel) -> dict[str, JsonValue]:
    assert isinstance(output, AboutYouRead)
    return output.converted().model_dump(mode="json")


_STEPS: dict[OnboardingStep, _StepReader] = {
    OnboardingStep.ABOUT_YOU: _StepReader(about_you_template(), _about_you_fields),
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
        return step.fields(result.output)


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
