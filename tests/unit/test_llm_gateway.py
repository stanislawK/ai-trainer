import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel, PostgresDsn, SecretStr
from pydantic_ai import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_trainer.domain.llm_calls import LlmCall, LlmCallOutcome, NewLlmCall
from ai_trainer.llm.gateway import ERROR_MESSAGE, TIMEOUT_MESSAGE, OpenRouterGateway
from ai_trainer.settings import Settings

_FROZEN_TIME = datetime(2026, 1, 1, tzinfo=UTC)


class Greeting(BaseModel):
    text: str


class FakeLlmCallsRepository:
    def __init__(self) -> None:
        self.recorded: list[NewLlmCall] = []

    async def record(self, call: NewLlmCall) -> LlmCall:
        self.recorded.append(call)
        return LlmCall(id=uuid4(), created_at=_FROZEN_TIME, **call.model_dump())

    async def list_for_user(self, user_id: UUID) -> list[LlmCall]:
        return [
            LlmCall(id=uuid4(), created_at=_FROZEN_TIME, **c.model_dump())
            for c in self.recorded
            if c.user_id == user_id
        ]


def _settings(timeout_seconds: float = 30.0) -> Settings:
    return Settings(
        _env_file=None,
        database_url=PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        openrouter_api_key=SecretStr("test-key"),
        eval_judge_model="test/judge-model",
        llm_call_timeout_seconds=timeout_seconds,
    )


def _success_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(
        parts=[TextPart('{"text": "hello"}')],
        provider_details={"cost": 0.0042},
    )


async def _hanging_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    await asyncio.sleep(10)
    return ModelResponse(parts=[TextPart('{"text": "too late"}')])


def _raising_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    raise RuntimeError("provider exploded")


def _no_cost_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart('{"text": "hi"}')])


def _invalid_then_valid_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if len(messages) == 1:
        # First attempt: malformed for `Greeting`'s schema, forcing pydantic-ai's default
        # one-retry-on-output-validation-failure behaviour (a second model request, still
        # within one `agent.run()` call — no tool calls involved).
        return ModelResponse(
            parts=[TextPart('{"not_text": "oops"}')], provider_details={"cost": 0.1}
        )
    # 0.1 + 0.2 == 0.30000000000000004 in binary float: exercises the summing-precision fix,
    # not just the retry-accumulation logic (0.001 + 0.002 would round-trip clean either way).
    return ModelResponse(parts=[TextPart('{"text": "hello"}')], provider_details={"cost": 0.2})


async def test_successful_call_returns_output_and_records_tokens_and_cost() -> None:
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), repository)
    user_id = uuid4()

    with gateway.agent.override(model=FunctionModel(_success_response)):
        result = await gateway.run(
            user_id=user_id,
            template_id="greeter",
            template_version=1,
            model_id="openai/gpt-5-mini",
            output_type=Greeting,
            instructions="Greet the user.",
            prompt="Say hello",
        )

    assert result.outcome is LlmCallOutcome.SUCCESS
    assert result.friendly_error is None
    assert result.output is not None
    assert result.output.text == "hello"

    assert len(repository.recorded) == 1
    recorded = repository.recorded[0]
    assert recorded.user_id == user_id
    assert recorded.template_id == "greeter"
    assert recorded.template_version == 1
    assert recorded.model == "openai/gpt-5-mini"
    assert recorded.outcome is LlmCallOutcome.SUCCESS
    assert recorded.input_tokens > 0
    assert recorded.output_tokens > 0
    assert recorded.cost == Decimal("0.0042")
    assert recorded.latency_ms >= 0


async def test_timeout_writes_a_timeout_row_and_returns_a_friendly_error() -> None:
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(timeout_seconds=0.01), repository)
    user_id = uuid4()

    with gateway.agent.override(model=FunctionModel(_hanging_response)):
        result = await gateway.run(
            user_id=user_id,
            template_id="greeter",
            template_version=1,
            model_id="openai/gpt-5-mini",
            output_type=Greeting,
            instructions="Greet the user.",
            prompt="Say hello",
        )

    assert result.outcome is LlmCallOutcome.TIMEOUT
    assert result.output is None
    assert result.friendly_error == TIMEOUT_MESSAGE

    assert len(repository.recorded) == 1
    recorded = repository.recorded[0]
    assert recorded.outcome is LlmCallOutcome.TIMEOUT
    assert recorded.input_tokens == 0
    assert recorded.output_tokens == 0
    assert recorded.cost is None


async def test_provider_error_writes_an_error_row_and_returns_a_friendly_error() -> None:
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), repository)
    user_id = uuid4()

    with gateway.agent.override(model=FunctionModel(_raising_response)):
        result = await gateway.run(
            user_id=user_id,
            template_id="greeter",
            template_version=1,
            model_id="openai/gpt-5-mini",
            output_type=Greeting,
            instructions="Greet the user.",
            prompt="Say hello",
        )

    assert result.outcome is LlmCallOutcome.ERROR
    assert result.output is None
    assert result.friendly_error == ERROR_MESSAGE

    assert len(repository.recorded) == 1
    assert repository.recorded[0].outcome is LlmCallOutcome.ERROR


async def test_output_validation_retry_sums_cost_across_both_requests() -> None:
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), repository)

    with gateway.agent.override(model=FunctionModel(_invalid_then_valid_response)):
        result = await gateway.run(
            user_id=uuid4(),
            template_id="greeter",
            template_version=1,
            model_id="openai/gpt-5-mini",
            output_type=Greeting,
            instructions="Greet the user.",
            prompt="Say hello",
        )

    assert result.outcome is LlmCallOutcome.SUCCESS
    assert result.output is not None
    assert result.output.text == "hello"
    # Both the failed and the retried request billed a cost; neither is dropped.
    assert repository.recorded[0].cost == Decimal("0.3")


async def test_missing_cost_in_provider_details_records_none() -> None:
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), repository)

    with gateway.agent.override(model=FunctionModel(_no_cost_response)):
        await gateway.run(
            user_id=uuid4(),
            template_id="greeter",
            template_version=1,
            model_id="openai/gpt-5-mini",
            output_type=Greeting,
            instructions="Greet the user.",
            prompt="Say hello",
        )

    assert repository.recorded[0].cost is None
