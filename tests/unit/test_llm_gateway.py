import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import anyio
from pydantic import BaseModel, PostgresDsn, SecretStr
from pydantic_ai import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_trainer.application.ports.llm_gateway import LlmStreamEvent, StreamEnd, TextChunk
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


async def _scripted_stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
    for chunk in ("Hel", "lo ", "there"):
        yield chunk


async def _hanging_stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
    await asyncio.sleep(10)
    yield "too late"


async def _raising_stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
    raise RuntimeError("provider exploded")
    yield "unreachable"


async def _collect(
    gateway: OpenRouterGateway, user_id: UUID, *, stop_after: int | None = None
) -> list[LlmStreamEvent]:
    events: list[LlmStreamEvent] = []
    async with aclosing(
        gateway.stream(
            user_id=user_id,
            template_id="chat_reply",
            template_version=2,
            model_id="openai/gpt-5-mini",
            instructions="Reply.",
            prompt="Say hello",
        )
    ) as stream:
        async for event in stream:
            events.append(event)
            if stop_after is not None and len(events) >= stop_after:
                break
    return events


async def test_gateway_stream_yields_chunks_in_order_then_one_success_row() -> None:
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), repository)
    user_id = uuid4()

    with gateway.agent.override(model=FunctionModel(stream_function=_scripted_stream)):
        events = await _collect(gateway, user_id)

    assert events[:-1] == [TextChunk("Hel"), TextChunk("lo "), TextChunk("there")]
    end = events[-1]
    assert isinstance(end, StreamEnd)
    assert end.result.outcome is LlmCallOutcome.SUCCESS
    assert end.result.output == "Hello there"
    assert end.result.friendly_error is None

    assert len(repository.recorded) == 1
    recorded = repository.recorded[0]
    assert recorded.outcome is LlmCallOutcome.SUCCESS
    assert recorded.user_id == user_id
    assert recorded.template_id == "chat_reply"
    assert recorded.template_version == 2
    assert recorded.model == "openai/gpt-5-mini"
    assert recorded.input_tokens > 0
    assert recorded.output_tokens > 0
    assert recorded.cost is None
    assert recorded.latency_ms >= 0


async def test_gateway_stream_timeout_ends_with_friendly_error_and_one_timeout_row() -> None:
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(timeout_seconds=0.01), repository)

    with gateway.agent.override(model=FunctionModel(stream_function=_hanging_stream)):
        events = await _collect(gateway, uuid4())

    assert len(events) == 1
    end = events[0]
    assert isinstance(end, StreamEnd)
    assert end.result.outcome is LlmCallOutcome.TIMEOUT
    assert end.result.output is None
    assert end.result.friendly_error == TIMEOUT_MESSAGE

    assert len(repository.recorded) == 1
    assert repository.recorded[0].outcome is LlmCallOutcome.TIMEOUT
    assert repository.recorded[0].input_tokens == 0
    assert repository.recorded[0].cost is None


async def test_gateway_stream_provider_error_ends_with_friendly_error_and_one_error_row() -> None:
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), repository)

    with gateway.agent.override(model=FunctionModel(stream_function=_raising_stream)):
        events = await _collect(gateway, uuid4())

    assert len(events) == 1
    end = events[0]
    assert isinstance(end, StreamEnd)
    assert end.result.outcome is LlmCallOutcome.ERROR
    assert end.result.friendly_error == ERROR_MESSAGE

    assert len(repository.recorded) == 1
    assert repository.recorded[0].outcome is LlmCallOutcome.ERROR


async def test_gateway_stream_consumer_stopping_early_still_leaves_one_cancelled_row() -> None:
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), repository)
    user_id = uuid4()

    with gateway.agent.override(model=FunctionModel(stream_function=_scripted_stream)):
        events = await _collect(gateway, user_id, stop_after=1)

    assert events == [TextChunk("Hel")]
    assert len(repository.recorded) == 1
    recorded = repository.recorded[0]
    assert recorded.outcome is LlmCallOutcome.CANCELLED
    assert recorded.user_id == user_id
    assert recorded.template_id == "chat_reply"
    assert recorded.model == "openai/gpt-5-mini"


async def _stall_after_two_chunks(
    messages: list[ModelMessage], info: AgentInfo
) -> AsyncIterator[str]:
    yield "Hel"
    yield "lo"
    await asyncio.sleep(10)
    yield "too late"


async def _fail_after_a_chunk(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
    yield "Hel"
    raise RuntimeError("provider exploded")


async def _empty_stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
    return
    yield "unreachable"


def _stream(gateway: OpenRouterGateway, user_id: UUID) -> AsyncIterator[LlmStreamEvent]:
    return gateway.stream(
        user_id=user_id,
        template_id="chat_reply",
        template_version=2,
        model_id="openai/gpt-5-mini",
        instructions="Reply.",
        prompt="Say hello",
    )


async def _wait_for_rows(repository: FakeLlmCallsRepository, count: int) -> None:
    async with asyncio.timeout(2):
        while len(repository.recorded) < count:
            await asyncio.sleep(0.01)


async def test_gateway_stream_timeout_after_chunks_keeps_them_and_the_usage_reported() -> None:
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(timeout_seconds=0.1), repository)

    with gateway.agent.override(model=FunctionModel(stream_function=_stall_after_two_chunks)):
        events = await _collect(gateway, uuid4())

    assert events[:2] == [TextChunk("Hel"), TextChunk("lo")]
    end = events[-1]
    assert isinstance(end, StreamEnd)
    assert end.result.outcome is LlmCallOutcome.TIMEOUT
    assert end.result.output is None
    assert len(repository.recorded) == 1
    assert repository.recorded[0].outcome is LlmCallOutcome.TIMEOUT
    assert repository.recorded[0].output_tokens > 0


async def test_gateway_stream_error_after_a_chunk_leaves_one_error_row() -> None:
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), repository)

    with gateway.agent.override(model=FunctionModel(stream_function=_fail_after_a_chunk)):
        events = await _collect(gateway, uuid4())

    assert events[0] == TextChunk("Hel")
    end = events[-1]
    assert isinstance(end, StreamEnd)
    assert end.result.friendly_error == ERROR_MESSAGE
    assert [c.outcome for c in repository.recorded] == [LlmCallOutcome.ERROR]


async def test_gateway_stream_empty_reply_is_an_error_row() -> None:
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), repository)

    with gateway.agent.override(model=FunctionModel(stream_function=_empty_stream)):
        events = await _collect(gateway, uuid4())

    end = events[-1]
    assert isinstance(end, StreamEnd)
    assert end.result.friendly_error == ERROR_MESSAGE
    assert [c.outcome for c in repository.recorded] == [LlmCallOutcome.ERROR]


async def test_gateway_stream_plain_break_without_closing_still_leaves_one_row() -> None:
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), repository)

    with gateway.agent.override(model=FunctionModel(stream_function=_scripted_stream)):
        async for _ in _stream(gateway, uuid4()):
            break
        await _wait_for_rows(repository, 1)
        await asyncio.sleep(0.05)

    assert [c.outcome for c in repository.recorded] == [LlmCallOutcome.CANCELLED]


async def test_gateway_stream_consumer_task_cancelled_between_chunks_leaves_one_row() -> None:
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), repository)
    got_first = asyncio.Event()

    async def consume() -> None:
        async for _ in _stream(gateway, uuid4()):
            got_first.set()
            await asyncio.sleep(10)

    with gateway.agent.override(model=FunctionModel(stream_function=_scripted_stream)):
        task = asyncio.create_task(consume())
        await got_first.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _wait_for_rows(repository, 1)
        await asyncio.sleep(0.05)

    assert [c.outcome for c in repository.recorded] == [LlmCallOutcome.CANCELLED]


async def _chunk_then_stall(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
    yield "Hel"
    await asyncio.sleep(10)
    yield "lo"


async def test_gateway_stream_disconnect_through_an_anyio_scope_still_leaves_one_row() -> None:
    """FastAPI's SSE route runs the reply in an anyio task group and cancels its scope when
    the browser leaves (#82). anyio re-delivers that cancellation at every await, so the
    bookkeeping in the stream's `finally` must not be cancelled with it (ADR-0018)."""
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), repository)

    async def consume() -> None:
        async for _ in _stream(gateway, uuid4()):
            pass

    with gateway.agent.override(model=FunctionModel(stream_function=_chunk_then_stall)):
        async with anyio.create_task_group() as group:
            group.start_soon(consume)
            await anyio.sleep(0.1)
            group.cancel_scope.cancel()
        await _wait_for_rows(repository, 1)

    assert [c.outcome for c in repository.recorded] == [LlmCallOutcome.CANCELLED]


async def test_gateway_run_disconnect_through_an_anyio_scope_still_leaves_one_row() -> None:
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), repository)
    user_id = uuid4()

    async def route() -> None:
        await gateway.run(
            user_id=user_id,
            template_id="router",
            template_version=1,
            model_id="openai/gpt-5-mini",
            output_type=Greeting,
            instructions="Be brief.",
            prompt="hi",
        )

    with gateway.agent.override(model=FunctionModel(_hanging_response)):
        async with anyio.create_task_group() as group:
            group.start_soon(route)
            await anyio.sleep(0.1)
            group.cancel_scope.cancel()
        await _wait_for_rows(repository, 1)

    [recorded] = repository.recorded
    assert (recorded.outcome, recorded.user_id, recorded.template_id) == (
        LlmCallOutcome.CANCELLED,
        user_id,
        "router",
    )


async def test_gateway_run_cancelled_once_records_the_call_and_stays_cancelled() -> None:
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), repository)

    with gateway.agent.override(model=FunctionModel(_hanging_response)):
        task = asyncio.create_task(
            gateway.run(
                user_id=uuid4(),
                template_id="router",
                template_version=1,
                model_id="openai/gpt-5-mini",
                output_type=Greeting,
                instructions="Be brief.",
                prompt="hi",
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(results[0], asyncio.CancelledError)
    assert [c.outcome for c in repository.recorded] == [LlmCallOutcome.CANCELLED]


async def test_run_sends_the_requested_temperature_to_the_model() -> None:
    seen: list[float | None] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append((info.model_settings or {}).get("temperature"))
        return _success_response(messages, info)

    gateway = OpenRouterGateway(_settings(), FakeLlmCallsRepository())

    with gateway.agent.override(model=FunctionModel(respond)):
        await gateway.run(
            user_id=uuid4(),
            template_id="greeter",
            template_version=1,
            model_id="openai/gpt-5-mini",
            output_type=Greeting,
            instructions="Greet the user.",
            prompt="Say hello",
            temperature=0,
        )

    assert seen == [0]


async def test_run_pins_the_requested_upstream_provider_and_keeps_fallbacks() -> None:
    seen: list[object] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append((info.model_settings or {}).get("openrouter_provider"))
        return _success_response(messages, info)

    gateway = OpenRouterGateway(_settings(), FakeLlmCallsRepository())

    with gateway.agent.override(model=FunctionModel(respond)):
        for upstream in ("z-ai", None):
            await gateway.run(
                user_id=uuid4(),
                template_id="greeter",
                template_version=1,
                model_id="openai/gpt-5-mini",
                output_type=Greeting,
                instructions="Greet the user.",
                prompt="Say hello",
                upstream_provider=upstream,
            )

    assert seen == [{"order": ["z-ai"], "allow_fallbacks": True}, None]


async def test_run_retries_an_invalid_output_as_often_as_requested() -> None:
    calls = 0

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(parts=[TextPart('{"wrong": "shape"}')])

    gateway = OpenRouterGateway(_settings(), FakeLlmCallsRepository())

    with gateway.agent.override(model=FunctionModel(respond)):
        result = await gateway.run(
            user_id=uuid4(),
            template_id="greeter",
            template_version=1,
            model_id="openai/gpt-5-mini",
            output_type=Greeting,
            instructions="Greet the user.",
            prompt="Say hello",
            output_retries=2,
        )

    assert result.outcome is LlmCallOutcome.ERROR
    assert calls == 3
