"""`exercise.simple_howto` (ADR-0031 *Beginner how-to*, #589): the template renders from public
catalogue text only; the writer runs it under the athlete with the template's settings, one
`llm_calls` row per call; its grader checks the kind, the length and the digits in code; the
committed smoke set holds its judge."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from pydantic import BaseModel, PostgresDsn, SecretStr, ValidationError
from pydantic_ai import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_evals.evaluators import EvaluatorContext

from ai_trainer.application.ports.llm_gateway import LlmGatewayResult
from ai_trainer.domain.exercise_simple_texts import (
    HowtoCallFailed,
    NoHowto,
    SimpleHowto,
    SimpleHowtoDeps,
    SimpleHowtoDraft,
    has_new_digit,
    invalid_reason,
)
from ai_trainer.domain.llm_calls import LlmCallOutcome, ProviderPreferences
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.conversation import build_prompt_registry
from ai_trainer.llm.evals import cli as cli_module
from ai_trainer.llm.evals.inputs import EvalCaseInputs
from ai_trainer.llm.evals.smoke import read_smoke_set
from ai_trainer.llm.gateway import OpenRouterGateway
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.simple_howto import (
    SIMPLE_HOWTO_TEMPLATE_ID,
    SIMPLE_HOWTO_VERSION,
    LlmSimpleHowtoWriter,
    SimpleHowtoCall,
    SimpleHowtoMatch,
    simple_howto_template,
)
from ai_trainer.settings import Settings
from ai_trainer.settings_values import DEFAULT_EXERCISE_SIMPLE_HOWTO_MAX_CHARS
from tests.unit.test_llm_gateway import FakeLlmCallsRepository

_ROOT = Path(__file__).parents[2]
_PROMPTS_ROOT = _ROOT / "src" / "ai_trainer" / "llm" / "prompts"
_SMOKE_ROOT = _ROOT / "evals" / "smoke"
_DEPS = SimpleHowtoDeps(
    exercise_name="Barbell Bench Press",
    instructions=(
        "Lie flat on a bench with your feet flat on the ground. Lower the barbell slowly towards "
        "your chest, keeping your elbows tucked in."
    ),
    target="pectorals",
    equipment="barbell",
    max_chars=160,
)
_CALL = SimpleHowtoCall(
    model="test/howto-model",
    provider=ProviderPreferences(sort="latency"),
    reasoning_effort="low",
    timeout_seconds=8.0,
)


def _registry() -> PromptRegistry:
    return build_prompt_registry(_PROMPTS_ROOT, default_sport_registry())


def _settings(timeout_seconds: float = 30.0) -> Settings:
    return Settings(
        _env_file=None,
        database_url=PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        openrouter_api_key=SecretStr("test-key"),
        eval_judge_model="test/judge-model",
        llm_call_timeout_seconds=timeout_seconds,
    )


def test_template_renders_from_public_text_with_no_identity() -> None:
    rendered = _registry().render_instructions(
        SIMPLE_HOWTO_TEMPLATE_ID, SIMPLE_HOWTO_VERSION, _DEPS
    )

    assert "Exercise: Barbell Bench Press" in rendered
    assert _DEPS.instructions in rendered
    assert "Target: pectorals" in rendered
    assert "Equipment: barbell" in rendered
    # The prompt states the limit the code enforces, from `EXERCISE_SIMPLE_HOWTO_MAX_CHARS`.
    assert "under 160" in rendered
    shorter = _DEPS.model_copy(update={"max_chars": 120})
    assert "under 120" in _registry().render_instructions(
        SIMPLE_HOWTO_TEMPLATE_ID, SIMPLE_HOWTO_VERSION, shorter
    )
    assert set(SimpleHowtoDeps.model_fields) == {
        "exercise_name",
        "instructions",
        "target",
        "equipment",
        "max_chars",
    }
    with pytest.raises(ValidationError):
        SimpleHowtoDeps.model_validate(_DEPS.model_dump() | {"user_id": "x"})
    bare = _DEPS.model_copy(update={"target": None, "equipment": None})
    rendered_bare = _registry().render_instructions(
        SIMPLE_HOWTO_TEMPLATE_ID, SIMPLE_HOWTO_VERSION, bare
    )
    assert "Target: not given" in rendered_bare


def test_template_reads_at_zero_with_no_persona_and_its_own_settings() -> None:
    template = simple_howto_template()

    assert template.persona is False
    assert template.temperature == 0
    assert template.output_type == (SimpleHowto, NoHowto)
    settings = _settings()
    assert getattr(settings, template.model_settings_key) == "z-ai/glm-5.3-flash"
    assert template.provider_settings_key == "simple_howto_provider_preferences"
    assert template.reasoning_effort_settings_key == "simple_howto_reasoning_effort"
    assert _registry().versions(SIMPLE_HOWTO_TEMPLATE_ID) == [SIMPLE_HOWTO_VERSION]


def test_body_keeps_every_cue_and_adds_nothing() -> None:
    body = (_PROMPTS_ROOT / SIMPLE_HOWTO_TEMPLATE_ID / "v1.en.md").read_text().lower()

    assert "keep every safety and form cue" in body
    assert "add nothing the instructions don't say" in body
    assert "nohowto" in body


@dataclass
class RecordingGateway:
    """Implements `LlmGatewayPort.run` only: remembers each call and returns `result`."""

    result: LlmGatewayResult[Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def run[OutputT: BaseModel](self, **kwargs: Any) -> LlmGatewayResult[OutputT]:
        self.calls.append(kwargs)
        return self.result

    def stream(self, **kwargs: Any) -> Any:  # pragma: no cover -- never streams
        raise AssertionError("the writer never streams")


def _writer(gateway: Any) -> LlmSimpleHowtoWriter:
    return LlmSimpleHowtoWriter(registry=_registry(), gateway=gateway, call=_CALL)


async def test_writer_runs_under_the_athlete_with_template_settings() -> None:
    gateway = RecordingGateway(
        LlmGatewayResult(
            output=SimpleHowto(text="Lie on the bench."),
            friendly_error=None,
            outcome=LlmCallOutcome.SUCCESS,
        )
    )
    athlete = uuid4()

    result = await _writer(gateway).write(user_id=athlete, deps=_DEPS)

    assert result == SimpleHowtoDraft(
        text="Lie on the bench.",
        template_id=SIMPLE_HOWTO_TEMPLATE_ID,
        template_version=SIMPLE_HOWTO_VERSION,
        model="test/howto-model",
    )
    [call] = gateway.calls
    assert call["user_id"] == athlete
    assert (call["template_id"], call["template_version"], call["model_id"]) == (
        SIMPLE_HOWTO_TEMPLATE_ID,
        SIMPLE_HOWTO_VERSION,
        "test/howto-model",
    )
    assert call["provider"] == ProviderPreferences(sort="latency")
    assert call["reasoning_effort"] == "low"
    assert call["timeout_seconds"] == 8.0
    assert call["temperature"] == 0
    assert call["output_type"] == (SimpleHowto, NoHowto)
    assert call["prompt"] == "Barbell Bench Press"
    assert _DEPS.instructions in call["instructions"]


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (LlmCallOutcome.TIMEOUT, "timeout"),
        (LlmCallOutcome.RATE_LIMITED, "rate_limited"),
        (LlmCallOutcome.ERROR, "error"),
    ],
    ids=["timeout", "rate_limited", "error"],
)
async def test_writer_turns_a_failed_call_into_its_reason(
    outcome: LlmCallOutcome, reason: str
) -> None:
    gateway = RecordingGateway(LlmGatewayResult(output=None, friendly_error="x", outcome=outcome))

    result = await _writer(gateway).write(user_id=uuid4(), deps=_DEPS)

    assert result == HowtoCallFailed(reason=reason)  # type: ignore[arg-type]  # parametrized literal


def _user_turn(messages: list[ModelMessage]) -> str:
    request = messages[0]
    assert isinstance(request, ModelRequest)
    [part] = [part for part in request.parts if isinstance(part, UserPromptPart)]
    return str(part.content)


async def test_writer_through_the_gateway_writes_one_row_under_the_athlete() -> None:
    def howto(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert _user_turn(messages) == "Barbell Bench Press"
        [tool] = [output.name for output in info.output_tools if "SimpleHowto" in output.name]
        return ModelResponse(parts=[ToolCallPart(tool, {"text": "Lie on the bench."})])

    calls = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), calls)
    athlete = uuid4()

    with gateway.agent.override(model=FunctionModel(howto)):
        result = await _writer(gateway).write(user_id=athlete, deps=_DEPS)

    assert isinstance(result, SimpleHowtoDraft)
    [row] = calls.recorded
    assert (row.user_id, row.template_id, row.template_version, row.model) == (
        athlete,
        SIMPLE_HOWTO_TEMPLATE_ID,
        SIMPLE_HOWTO_VERSION,
        "test/howto-model",
    )


async def test_writer_returns_the_models_refusal() -> None:
    def no_howto(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        [tool] = [output.name for output in info.output_tools if "NoHowto" in output.name]
        return ModelResponse(parts=[ToolCallPart(tool, {"reason": "it doesn't say how"})])

    gateway = OpenRouterGateway(_settings(), FakeLlmCallsRepository())

    with gateway.agent.override(model=FunctionModel(no_howto)):
        result = await _writer(gateway).write(user_id=uuid4(), deps=_DEPS)

    assert result == NoHowto(reason="it doesn't say how")


async def test_a_slow_model_is_a_timeout() -> None:
    async def slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(10)
        raise AssertionError("cancelled by the timeout")  # pragma: no cover

    gateway = OpenRouterGateway(_settings(), FakeLlmCallsRepository())
    writer = LlmSimpleHowtoWriter(
        registry=_registry(),
        gateway=gateway,
        call=SimpleHowtoCall(
            model="test/howto-model", provider=None, reasoning_effort=None, timeout_seconds=0.01
        ),
    )

    with gateway.agent.override(model=FunctionModel(slow)):
        result = await writer.write(user_id=uuid4(), deps=_DEPS)

    assert result == HowtoCallFailed(reason="timeout")


def _ctx(
    output: SimpleHowto | NoHowto, expected: SimpleHowto | NoHowto
) -> EvaluatorContext[EvalCaseInputs, Any]:
    return EvaluatorContext(
        name="case",
        inputs=EvalCaseInputs(prompt=_DEPS.exercise_name, deps=_DEPS.model_dump()),
        metadata=None,
        expected_output=expected,
        output=output,
        duration=0.0,
        _span_tree=None,  # type: ignore[arg-type]  # not read by this evaluator
        attributes={},
        metrics={},
    )


def test_simple_howto_match_grades_length_and_digits() -> None:
    evaluator = SimpleHowtoMatch()
    good = SimpleHowto(text="Lie on the bench, feet flat. Lower the bar slowly, elbows tucked in.")

    assert evaluator.evaluate(_ctx(good, good)) == {
        "kind": True,
        "length": True,
        "no_new_digit": True,
    }
    long = evaluator.evaluate(
        _ctx(SimpleHowto(text="x" * (DEFAULT_EXERCISE_SIMPLE_HOWTO_MAX_CHARS + 1)), good)
    )
    assert long["length"] is False  # type: ignore[index]
    empty = evaluator.evaluate(_ctx(SimpleHowto(text="  "), good))
    assert empty["length"] is False  # type: ignore[index]
    sets = evaluator.evaluate(_ctx(SimpleHowto(text="Do 3 sets of 10."), good))
    assert sets["no_new_digit"] is False  # type: ignore[index]
    assert evaluator.evaluate(_ctx(NoHowto(reason="none"), good)) == {"kind": False}
    assert evaluator.evaluate(_ctx(good, NoHowto(reason="none"))) == {"kind": False}
    assert evaluator.evaluate(_ctx(NoHowto(reason="a"), NoHowto(reason="b"))) == {"kind": True}


def test_digits_rule_reads_the_whole_source() -> None:
    deps = SimpleHowtoDeps(
        exercise_name="3/4 Sit-Up",
        instructions="Curl up until your torso is at a 45-degree angle.",
        target="abs",
        equipment="body weight",
        max_chars=160,
    )

    assert not has_new_digit("A 3/4 sit-up: curl up to 45 degrees.", deps)
    assert has_new_digit("Curl up to 90 degrees.", deps)
    # The length limit is a setting, not source text: it never licenses a number.
    assert has_new_digit("Curl up 160 times.", deps)
    assert invalid_reason(" Curl up to 45 degrees. ", deps, max_chars=160) is None


def test_the_committed_smoke_set_is_judged_and_its_references_pass_the_code_checks() -> None:
    smoke_set = read_smoke_set(
        SIMPLE_HOWTO_TEMPLATE_ID,
        smoke_root=_SMOKE_ROOT,
        known_templates=cli_module._active_versions(default_sport_registry()),
    )
    raw = yaml.safe_load((_SMOKE_ROOT / f"{SIMPLE_HOWTO_TEMPLATE_ID}.yaml").read_text())

    assert smoke_set.has_judge
    assert len(smoke_set.cases) >= 5
    assert raw["evaluators"] == ["SimpleHowtoMatch"]
    for case in raw["cases"]:
        [judge] = [entry["LLMJudge"] for entry in case["evaluators"]]
        assert judge["include_input"] is True, case["name"]
        assert "keeps every safety and form cue" in judge["rubric"], case["name"]
        assert "adds nothing the instructions don't say" in judge["rubric"], case["name"]
        deps = SimpleHowtoDeps.model_validate(case["inputs"]["deps"])
        assert case["inputs"]["prompt"] == deps.exercise_name
        reference = case["expected_output"]["text"]
        assert invalid_reason(reference, deps, max_chars=160) is None, case["name"]
    assert any(
        any(c.isdigit() for c in case["inputs"]["deps"]["instructions"]) for case in raw["cases"]
    )


def _smoke_yaml() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(
        (_SMOKE_ROOT / f"{SIMPLE_HOWTO_TEMPLATE_ID}.yaml").read_text()
    )
    return raw


def test_smoke_cases_state_the_settings_cap() -> None:
    # #600: the set grades the cap the app enforces, and every reference fits under it.
    for case in _smoke_yaml()["cases"]:
        deps = SimpleHowtoDeps.model_validate(case["inputs"]["deps"])
        assert deps.max_chars == DEFAULT_EXERCISE_SIMPLE_HOWTO_MAX_CHARS, case["name"]
        reference = case["expected_output"]["text"]
        assert len(reference) <= DEFAULT_EXERCISE_SIMPLE_HOWTO_MAX_CHARS, case["name"]


def test_smoke_set_has_no_known_gaps_for_600() -> None:
    known_gaps: dict[str, str] = _smoke_yaml().get("known_gaps") or {}
    assert [name for name, ticket in known_gaps.items() if ticket == "#600"] == []


def test_the_evals_cli_grades_the_set_in_code_at_the_apps_version() -> None:
    assert cli_module._evaluators_for(SIMPLE_HOWTO_TEMPLATE_ID) == (SimpleHowtoMatch,)
    versions = cli_module._active_versions(default_sport_registry())
    assert versions[SIMPLE_HOWTO_TEMPLATE_ID] == SIMPLE_HOWTO_VERSION
