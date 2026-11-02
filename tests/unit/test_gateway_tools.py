"""A streamed reply that calls tools first (ADR-0010, ticket #421): it runs to the end however the
model orders its text and tool calls, streams as it goes, sends the session's user through the
real hook, and leaves one `llm_calls` row however it ends."""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx2
import pytest
from fastmcp import FastMCP
from fastmcp.server.context import Context
from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from ai_trainer.application.ports.llm_gateway import (
    LlmStreamEvent,
    StreamEnd,
    TextChunk,
    ToolLoop,
)
from ai_trainer.domain.llm_calls import LlmCallOutcome, ProviderPreferences
from ai_trainer.llm.gateway import OpenRouterGateway
from ai_trainer.mcp.identity import current_user_id
from ai_trainer.mcp.toolset import build_toolset
from tests.unit.test_llm_gateway import FakeLlmCallsRepository, _settings

_USER = uuid4()
_LOOP = ToolLoop(max_requests=4)

type _Stream = AsyncIterator[str | DeltaToolCalls]


def _server(seen: list[UUID]) -> FastMCP:
    server = FastMCP("test", mask_error_details=True)

    @server.tool
    async def lookup(ctx: Context) -> str:
        """Says what the athlete lifted."""
        seen.append(current_user_id(ctx))
        return "45 kg"

    @server.tool
    async def broken(ctx: Context) -> str:
        """Always fails."""
        raise RuntimeError(f"connection lost for {current_user_id(ctx)}")

    return server


def _gateway(
    repository: FakeLlmCallsRepository, seen: list[UUID] | None = None
) -> OpenRouterGateway:
    return OpenRouterGateway(
        _settings(), repository, toolset=build_toolset(_server(seen if seen is not None else []))
    )


def _tool_call(name: str, call_id: str = "1") -> DeltaToolCalls:
    return {0: DeltaToolCall(name=name, json_args="{}", tool_call_id=call_id)}


def _returned(messages: list[ModelMessage]) -> list[object]:
    return [
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


def _stream(gateway: OpenRouterGateway, **kwargs: Any) -> AsyncGenerator[LlmStreamEvent]:
    return gateway.stream(
        user_id=_USER,
        template_id="gym.answer",
        template_version=1,
        model_id="z-ai/glm-5.3-flash",
        instructions="Answer.",
        prompt="How is my bench going?",
        tool_loop=_LOOP,
        **kwargs,
    )


async def _collect(stream: AsyncGenerator[LlmStreamEvent]) -> tuple[list[str], StreamEnd]:
    chunks: list[str] = []
    end: StreamEnd | None = None
    async with aclosing(stream):
        async for event in stream:
            if isinstance(event, TextChunk):
                chunks.append(event.text)
            else:
                end = event
    assert end is not None
    return chunks, end


async def _tool_then_answer(messages: list[ModelMessage], info: AgentInfo) -> _Stream:
    if _returned(messages):
        yield "Your bench "
        yield "is 45 kg."
    else:
        yield _tool_call("lookup")


async def test_tool_stream_yields_text_as_it_comes() -> None:
    gateway = _gateway(FakeLlmCallsRepository())

    with gateway.tool_agent.override(model=FunctionModel(stream_function=_tool_then_answer)):
        chunks, end = await _collect(_stream(gateway))

    assert chunks == ["Your bench ", "is 45 kg."]
    assert end.result.output == "Your bench is 45 kg."
    assert end.result.outcome is LlmCallOutcome.SUCCESS


async def test_text_before_a_tool_call_does_not_end_the_run() -> None:
    """`run_stream` would take "Let me check." as the final output and never run the tool."""
    seen: list[UUID] = []

    async def speaks_first(messages: list[ModelMessage], info: AgentInfo) -> _Stream:
        if _returned(messages):
            yield "It is 45 kg."
        else:
            yield "Let me check."
            yield _tool_call("lookup")

    gateway = _gateway(FakeLlmCallsRepository(), seen)

    with gateway.tool_agent.override(model=FunctionModel(stream_function=speaks_first)):
        chunks, end = await _collect(_stream(gateway))

    assert seen == [_USER]
    assert chunks == ["Let me check.", "\n\n", "It is 45 kg."]
    assert end.result.output == "Let me check.\n\nIt is 45 kg."


async def test_the_tool_reads_the_user_the_gateway_was_given() -> None:
    seen: list[UUID] = []
    gateway = _gateway(FakeLlmCallsRepository(), seen)

    with gateway.tool_agent.override(model=FunctionModel(stream_function=_tool_then_answer)):
        await _collect(_stream(gateway))

    assert seen == [_USER]


async def test_one_row_sums_every_request_in_the_loop() -> None:
    async def answers_at_once(messages: list[ModelMessage], info: AgentInfo) -> _Stream:
        yield "Your bench "
        yield "is 45 kg."

    direct = FakeLlmCallsRepository()
    looped = FakeLlmCallsRepository()
    direct_gateway = _gateway(direct)
    looped_gateway = _gateway(looped)

    with direct_gateway.tool_agent.override(model=FunctionModel(stream_function=answers_at_once)):
        await _collect(_stream(direct_gateway))
    with looped_gateway.tool_agent.override(model=FunctionModel(stream_function=_tool_then_answer)):
        await _collect(_stream(looped_gateway))

    (row,) = looped.recorded
    assert row.outcome is LlmCallOutcome.SUCCESS
    assert row.template_id == "gym.answer"
    assert row.user_id == _USER
    assert row.input_tokens > direct.recorded[0].input_tokens


def _sse(*events: dict[str, object]) -> httpx2.Response:
    lines = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return httpx2.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=f"{lines}data: [DONE]\n\n".encode(),
    )


def _chunk(delta: dict[str, object], finish: str | None, cost: float) -> dict[str, object]:
    return {
        "id": "gen-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "z-ai/glm-5.3-flash",
        "provider": "Wafer",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": cost},
    }


async def test_one_row_sums_the_cost_openrouter_reports_for_each_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The request reaches only the mock transport, never OpenRouter (ADR-0013).
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    requests: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.content)
        if len(requests) == 1:
            call = {
                "index": 0,
                "id": "call-1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }
            return _sse(
                _chunk({"role": "assistant", "tool_calls": [call]}, "tool_calls", cost=0.001)
            )
        return _sse(_chunk({"role": "assistant", "content": "It is 45 kg."}, "stop", cost=0.002))

    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(
        _settings(),
        repository,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        toolset=build_toolset(_server([])),
    )

    _, end = await _collect(_stream(gateway))

    assert end.result.output == "It is 45 kg."
    assert len(requests) == 2
    (row,) = repository.recorded
    assert row.cost == Decimal("0.003")
    assert row.input_tokens == 20
    assert row.output_tokens == 10


async def test_one_row_on_error_when_the_model_fails() -> None:
    repository = FakeLlmCallsRepository()
    gateway = _gateway(repository)

    async def fails(messages: list[ModelMessage], info: AgentInfo) -> _Stream:
        nothing: tuple[str, ...] = ()
        for chunk in nothing:
            yield chunk
        raise RuntimeError("upstream is down")

    with gateway.tool_agent.override(model=FunctionModel(stream_function=fails)):
        chunks, end = await _collect(_stream(gateway))

    assert chunks == []
    assert end.result.output is None
    assert end.result.outcome is LlmCallOutcome.ERROR
    assert [row.outcome for row in repository.recorded] == [LlmCallOutcome.ERROR]


async def test_one_row_on_timeout_when_the_model_stalls() -> None:
    repository = FakeLlmCallsRepository()
    gateway = _gateway(repository)

    async def stalls(messages: list[ModelMessage], info: AgentInfo) -> _Stream:
        await asyncio.sleep(10)
        yield "too late"

    with gateway.tool_agent.override(model=FunctionModel(stream_function=stalls)):
        _, end = await _collect(_stream(gateway, timeout_seconds=0.01))

    assert end.result.outcome is LlmCallOutcome.TIMEOUT
    assert [row.outcome for row in repository.recorded] == [LlmCallOutcome.TIMEOUT]


async def test_a_tool_that_keeps_failing_ends_as_an_error_and_leaks_nothing() -> None:
    repository = FakeLlmCallsRepository()
    gateway = _gateway(repository)
    told: list[str] = []

    async def keeps_calling(messages: list[ModelMessage], info: AgentInfo) -> _Stream:
        told.append(str(messages[-1]))
        yield _tool_call("broken", str(len(messages)))

    with gateway.tool_agent.override(model=FunctionModel(stream_function=keeps_calling)):
        _, end = await _collect(_stream(gateway))

    assert end.result.outcome is LlmCallOutcome.ERROR
    assert end.result.output is None
    assert [row.outcome for row in repository.recorded] == [LlmCallOutcome.ERROR]
    assert not any(str(_USER) in text or "connection lost" in text for text in told)


async def test_the_request_cap_ends_the_loop_as_an_error_with_one_row() -> None:
    repository = FakeLlmCallsRepository()
    gateway = _gateway(repository)
    requests: list[int] = []

    async def loops(messages: list[ModelMessage], info: AgentInfo) -> _Stream:
        requests.append(len(messages))
        yield _tool_call("lookup", str(len(messages)))

    with gateway.tool_agent.override(model=FunctionModel(stream_function=loops)):
        _, end = await _collect(_stream(gateway))

    assert len(requests) == _LOOP.max_requests
    assert end.result.outcome is LlmCallOutcome.ERROR
    (row,) = repository.recorded
    assert row.outcome is LlmCallOutcome.ERROR
    # What the requests before the cap used is still accounted for.
    assert row.input_tokens > 0


async def test_a_loop_that_ends_with_no_text_is_an_error() -> None:
    repository = FakeLlmCallsRepository()
    gateway = _gateway(repository)

    async def says_nothing(messages: list[ModelMessage], info: AgentInfo) -> _Stream:
        if _returned(messages):
            yield "   "
        else:
            yield _tool_call("lookup")

    with gateway.tool_agent.override(model=FunctionModel(stream_function=says_nothing)):
        _, end = await _collect(_stream(gateway))

    assert end.result.output is None
    assert end.result.outcome is LlmCallOutcome.ERROR


async def test_a_consumer_stopping_early_leaves_one_cancelled_row() -> None:
    repository = FakeLlmCallsRepository()
    gateway = _gateway(repository)

    with gateway.tool_agent.override(model=FunctionModel(stream_function=_tool_then_answer)):
        async with aclosing(_stream(gateway)) as stream:
            async for _ in stream:
                break

    assert [row.outcome for row in repository.recorded] == [LlmCallOutcome.CANCELLED]


async def test_a_gateway_with_no_toolset_fails_a_tool_stream_as_an_error() -> None:
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(), repository)

    with gateway.tool_agent.override(model=FunctionModel(stream_function=_tool_then_answer)):
        _, end = await _collect(_stream(gateway))

    assert end.result.outcome is LlmCallOutcome.ERROR
    assert [row.outcome for row in repository.recorded] == [LlmCallOutcome.ERROR]


async def test_a_tool_stream_sends_its_routing_and_effort_in_the_request_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    bodies: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        bodies.append(request.content)
        return httpx2.Response(500)

    gateway = OpenRouterGateway(
        _settings(),
        FakeLlmCallsRepository(),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        toolset=build_toolset(_server([])),
    )

    await _collect(
        _stream(
            gateway,
            provider=ProviderPreferences(sort="latency"),
            reasoning_effort="low",
            session_id="conversation-key",
        )
    )

    assert bodies
    body = json.loads(bodies[0])
    assert body["provider"]["sort"] == "latency"
    assert body["reasoning"] == {"effort": "low"}
    assert body["session_id"] == "conversation-key"
    assert "lookup" in {tool["function"]["name"] for tool in body["tools"]}
