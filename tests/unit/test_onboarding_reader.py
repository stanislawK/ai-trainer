"""`onboarding.about_you` and its reader (PRD-0007 B34, ADR-0028, ticket #254): the answer is
read with the onboarding settings group, values come back with their units and code converts
them, and every read writes an `llm_calls` row under the athlete."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import PostgresDsn, SecretStr
from pydantic_ai import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_evals.evaluators import EvaluatorContext

from ai_trainer.domain.athlete_profile import AboutYou, HeightUnit, WeightUnit
from ai_trainer.domain.llm_calls import LlmCallOutcome, ProviderPreferences
from ai_trainer.domain.onboarding import AnswerStatus, OnboardingAnswer, OnboardingStep
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.conversation import build_prompt_registry
from ai_trainer.llm.evals.inputs import EvalCaseInputs
from ai_trainer.llm.gateway import OpenRouterGateway
from ai_trainer.llm.onboarding import (
    ABOUT_YOU_TEMPLATE_ID,
    AboutYouMatch,
    AboutYouRead,
    LlmOnboardingReader,
    OnboardingDeps,
    ReadHeight,
    ReadWeight,
)
from ai_trainer.settings import Settings
from tests.unit.test_llm_gateway import FakeLlmCallsRepository

_PROMPTS_ROOT = Path(__file__).parents[2] / "src" / "ai_trainer" / "llm" / "prompts"
# 23:30 UTC on 31 Dec is already New Year's Day in Warsaw (ADR-0014).
_NOW = datetime(2026, 12, 31, 23, 30, tzinfo=UTC)
_AC_ANSWER = "31, 5 ft 10, 160 lb, sore left shoulder"


class FakeClock:
    def now(self) -> datetime:
        return _NOW


def _answer(text: str = _AC_ANSWER) -> OnboardingAnswer:
    return OnboardingAnswer(
        step=OnboardingStep.ABOUT_YOU,
        sport_id=None,
        text=text,
        revision=1,
        status=AnswerStatus.PENDING,
    )


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url=PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        openrouter_api_key=SecretStr("test-key"),
        eval_judge_model="test/judge-model",
    )


def _read_ac_answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    read = {
        "age": 31,
        "height": {"value": 5, "unit": "ft_in", "inches": 10},
        "weight": {"value": 160, "unit": "lb"},
        "limits": "Sore left shoulder",
    }
    return ModelResponse(parts=[TextPart(json.dumps(read))], provider_details={"cost": 0.0001})


def _reader(gateway: OpenRouterGateway, **settings: object) -> LlmOnboardingReader:
    return LlmOnboardingReader(
        registry=build_prompt_registry(_PROMPTS_ROOT, default_sport_registry()),
        gateway=gateway,
        clock=FakeClock(),
        model="test/onboarding-model",
        **settings,  # type: ignore[arg-type]
    )


async def test_the_ac_answer_reads_as_31_178_cm_73_kg_and_the_shoulder() -> None:
    """AC: "31, 5 ft 10, 160 lb, sore left shoulder" shows 31, 178 cm, 73 kg, shoulder under
    limits."""
    gateway = OpenRouterGateway(_settings(), FakeLlmCallsRepository())
    with gateway.agent.override(model=FunctionModel(_read_ac_answer)):
        parsed = await _reader(gateway).read(
            uuid4(), _answer(), sport_id="climbing", timezone="Europe/Warsaw"
        )

    assert parsed == {
        "age": 31,
        "height_cm": 178,
        "weight_kg": 73.0,
        "limits": "Sore left shoulder",
    }


async def test_every_read_writes_one_llm_calls_row_under_the_athlete() -> None:
    calls = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), calls)
    athlete = uuid4()
    with gateway.agent.override(model=FunctionModel(_read_ac_answer)):
        await _reader(gateway).read(athlete, _answer(), sport_id="gym", timezone="UTC")

    [row] = calls.recorded
    assert (row.user_id, row.template_id, row.template_version, row.model) == (
        athlete,
        ABOUT_YOU_TEMPLATE_ID,
        1,
        "test/onboarding-model",
    )
    assert row.outcome is LlmCallOutcome.SUCCESS


async def test_a_failed_read_returns_nothing() -> None:
    def _raising(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("provider exploded")

    calls = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), calls)
    with gateway.agent.override(model=FunctionModel(_raising)):
        parsed = await _reader(gateway).read(uuid4(), _answer(), sport_id="gym", timezone="UTC")

    assert parsed is None
    assert [row.outcome for row in calls.recorded] == [LlmCallOutcome.ERROR]


async def test_the_read_sends_the_answer_and_the_onboarding_settings() -> None:
    sent: dict[str, object] = {}

    class RecordingGateway:
        async def run(self, **call: object) -> object:
            sent.update(call)
            from ai_trainer.application.ports.llm_gateway import LlmGatewayResult

            return LlmGatewayResult(
                output=AboutYouRead(), friendly_error=None, outcome=LlmCallOutcome.SUCCESS
            )

    preferences = ProviderPreferences(sort="price")
    reader = LlmOnboardingReader(
        registry=build_prompt_registry(_PROMPTS_ROOT, default_sport_registry()),
        gateway=RecordingGateway(),  # type: ignore[arg-type]
        clock=FakeClock(),
        model="test/onboarding-model",
        provider=preferences,
        reasoning_effort="low",
        timeout_seconds=20,
    )

    parsed = await reader.read(uuid4(), _answer("I'm 40"), sport_id="cycling", timezone="UTC")

    assert parsed == {"age": None, "height_cm": None, "weight_kg": None, "limits": None}
    assert sent["prompt"] == "I'm 40"
    assert sent["template_id"] == ABOUT_YOU_TEMPLATE_ID
    assert sent["model_id"] == "test/onboarding-model"
    assert sent["provider"] == preferences
    assert sent["reasoning_effort"] == "low"
    assert sent["timeout_seconds"] == 20
    assert sent["temperature"] == 0
    instructions = sent["instructions"]
    assert isinstance(instructions, str)
    assert "cycling" in instructions
    assert "2026-12-31" in instructions


async def test_today_is_the_athletes_own_day() -> None:
    """At 23:30 UTC on 31 December it is already 1 January in Warsaw."""
    rendered = build_prompt_registry(_PROMPTS_ROOT, default_sport_registry()).render_instructions(
        ABOUT_YOU_TEMPLATE_ID,
        1,
        OnboardingDeps(sport="climbing", today=date(2027, 1, 1), timezone="Europe/Warsaw"),
    )
    assert "2027-01-01" in rendered
    assert "Europe/Warsaw" in rendered

    sent: dict[str, object] = {}

    class RecordingGateway:
        async def run(self, **call: object) -> object:
            sent.update(call)
            from ai_trainer.application.ports.llm_gateway import LlmGatewayResult

            return LlmGatewayResult(output=None, friendly_error="x", outcome=LlmCallOutcome.TIMEOUT)

    reader = LlmOnboardingReader(
        registry=build_prompt_registry(_PROMPTS_ROOT, default_sport_registry()),
        gateway=RecordingGateway(),  # type: ignore[arg-type]
        clock=FakeClock(),
        model="m",
    )
    assert (
        await reader.read(uuid4(), _answer(), sport_id="climbing", timezone="Europe/Warsaw") is None
    )
    instructions = sent["instructions"]
    assert isinstance(instructions, str)
    assert "2027-01-01" in instructions


def test_the_read_converts_metres_and_kilograms_and_bounds_the_values() -> None:
    assert AboutYouRead(
        age=150,
        height=ReadHeight(value=1.78, unit=HeightUnit.M),
        weight=ReadWeight(value=72.46, unit=WeightUnit.KG),
        limits="  ",
    ).converted() == AboutYou(age=None, height_cm=178, weight_kg=72.5, limits=None)


def _ctx(output: AboutYouRead, expected: AboutYouRead) -> EvaluatorContext[EvalCaseInputs, object]:
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


def test_the_eval_grades_converted_values_so_any_unit_for_the_same_height_passes() -> None:
    expected = AboutYouRead(
        age=31,
        height=ReadHeight(value=5, unit=HeightUnit.FT_IN, inches=10),
        weight=ReadWeight(value=160, unit=WeightUnit.LB),
        limits="Sore left shoulder",
    )
    output = AboutYouRead(
        age=31,
        height=ReadHeight(value=178, unit=HeightUnit.CM),
        weight=ReadWeight(value=73, unit=WeightUnit.KG),
        limits="left shoulder, sore",
    )

    assert AboutYouMatch().evaluate(_ctx(output, expected)) == {  # type: ignore[arg-type]
        "age": True,
        "height_cm": True,
        "weight_kg": True,
        "limits_present": True,
    }


def test_the_eval_fails_a_guessed_value() -> None:
    expected = AboutYouRead(age=31)
    output = AboutYouRead(age=31, weight=ReadWeight(value=70, unit=WeightUnit.KG), limits="knee")

    assert AboutYouMatch().evaluate(_ctx(output, expected)) == {  # type: ignore[arg-type]
        "age": True,
        "height_cm": True,
        "weight_kg": False,
        "limits_present": False,
    }
