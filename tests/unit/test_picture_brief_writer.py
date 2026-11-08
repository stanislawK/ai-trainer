"""`media.picture_brief` through the text gateway (#533): the template rendered as its smoke set
renders it, the exercise's name as the user turn, one `llm_calls` row under the admin, and a
failed call a returned `BriefFailed`."""

import asyncio
from pathlib import Path
from uuid import uuid4

from pydantic import PostgresDsn, SecretStr
from pydantic_ai import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.media_generation import (
    BriefFailed,
    NoBrief,
    PictureBrief,
    PictureBriefDeps,
)
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.conversation import build_prompt_registry
from ai_trainer.llm.gateway import OpenRouterGateway
from ai_trainer.llm.picture_brief import (
    PICTURE_BRIEF_TEMPLATE_ID,
    PICTURE_BRIEF_VERSION,
    LlmPictureBriefWriter,
)
from ai_trainer.settings import Settings
from tests.unit.test_llm_gateway import FakeLlmCallsRepository

_PROMPTS_ROOT = Path(__file__).parents[2] / "src" / "ai_trainer" / "llm" / "prompts"
_DEPS = PictureBriefDeps(
    exercise_name="Pull-Up",
    description="Hang from a bar with your arms fully extended, then pull your chest to it.",
    passages=[],
)
_BRIEF = {
    "exercise_name": "Pull-Up",
    "start_position": "Hanging, arms extended",
    "end_position": "Chest at the bar",
    "image_prompt": "One person hanging, then chest at the bar. Plain background. No text.",
}


def _settings(timeout_seconds: float = 30.0) -> Settings:
    return Settings(
        _env_file=None,
        database_url=PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        openrouter_api_key=SecretStr("test-key"),
        eval_judge_model="test/judge-model",
        llm_call_timeout_seconds=timeout_seconds,
    )


def _writer(gateway: OpenRouterGateway) -> LlmPictureBriefWriter:
    return LlmPictureBriefWriter(
        registry=build_prompt_registry(_PROMPTS_ROOT, default_sport_registry()),
        gateway=gateway,
        model="test/brief-model",
    )


def _user_turn(messages: list[ModelMessage]) -> str:
    request = messages[0]
    assert isinstance(request, ModelRequest)
    [part] = [part for part in request.parts if isinstance(part, UserPromptPart)]
    return str(part.content)


async def test_writes_the_brief_from_the_rendered_template_as_the_admin() -> None:
    seen: list[tuple[str, str]] = []

    def brief(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append((str(messages[0].instructions), _user_turn(messages)))  # type: ignore[union-attr]
        [tool] = [output.name for output in info.output_tools if "PictureBrief" in output.name]
        return ModelResponse(parts=[ToolCallPart(tool, _BRIEF)], provider_details={"cost": 0.0002})

    calls = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), calls)
    admin = uuid4()

    with gateway.agent.override(model=FunctionModel(brief)):
        result = await _writer(gateway).write(user_id=admin, deps=_DEPS)

    assert result == PictureBrief.model_validate(_BRIEF)
    [(instructions, user_turn)] = seen
    assert "Exercise: Pull-Up" in instructions
    assert _DEPS.description is not None
    assert _DEPS.description in instructions
    assert user_turn == "Pull-Up"
    [row] = calls.recorded
    assert (row.user_id, row.template_id, row.template_version, row.model) == (
        admin,
        PICTURE_BRIEF_TEMPLATE_ID,
        PICTURE_BRIEF_VERSION,
        "test/brief-model",
    )


async def test_a_no_brief_reply_comes_back_as_no_brief() -> None:
    def no_brief(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        [tool] = [output.name for output in info.output_tools if "NoBrief" in output.name]
        return ModelResponse(parts=[ToolCallPart(tool, {"reason": "it doesn't say how"})])

    gateway = OpenRouterGateway(_settings(), FakeLlmCallsRepository())

    with gateway.agent.override(model=FunctionModel(no_brief)):
        result = await _writer(gateway).write(user_id=uuid4(), deps=_DEPS)

    assert result == NoBrief(reason="it doesn't say how")


async def test_a_failing_model_is_an_error() -> None:
    def failing(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("upstream down")

    calls = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), calls)

    with gateway.agent.override(model=FunctionModel(failing)):
        result = await _writer(gateway).write(user_id=uuid4(), deps=_DEPS)

    assert result == BriefFailed(reason="error")
    assert [row.outcome for row in calls.recorded] == [LlmCallOutcome.ERROR]


async def test_a_slow_model_is_a_timeout() -> None:
    async def slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(10)
        raise AssertionError("cancelled by the timeout")  # pragma: no cover

    gateway = OpenRouterGateway(_settings(timeout_seconds=0.01), FakeLlmCallsRepository())

    with gateway.agent.override(model=FunctionModel(slow)):
        result = await _writer(gateway).write(user_id=uuid4(), deps=_DEPS)

    assert result == BriefFailed(reason="timeout")
