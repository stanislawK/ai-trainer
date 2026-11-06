"""`cite_sources` through FastMCP's in-memory `Client(server)` (ADR-0010, ADR-0036 #428): it
acknowledges which passages a reply uses and changes nothing. Which of them count is code's job
after the run (`mcp/citations.py`), so the tool holds no state. A missing identity is a
`ToolError`, and arguments the schema refuses leave the run recoverable."""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_trainer.mcp.identity import USER_ID_META_KEY
from ai_trainer.mcp.results import ToolResult
from ai_trainer.mcp.server import CITE_SOURCES_TOOL, MAX_CITED
from ai_trainer.mcp.toolset import build_toolset
from tests.mcp_support import knowledge_server

_ATHLETE = UUID("00000000-0000-4000-8000-000000000428")


class _Cited(BaseModel):
    cited: int


async def _call(**args: Any) -> ToolResult[_Cited]:
    async with Client(knowledge_server()) as client:
        result = await client.call_tool(
            CITE_SOURCES_TOOL, args, meta={USER_ID_META_KEY: str(_ATHLETE)}
        )
    return ToolResult[_Cited].model_validate(result.structured_content)


async def test_cite_sources_acknowledges_how_many_passages_were_named() -> None:
    result = await _call(chunk_ids=[str(uuid4()), str(uuid4())])

    assert result == ToolResult[_Cited](success=True, data=_Cited(cited=2))


async def test_the_same_passage_named_twice_counts_once() -> None:
    chunk_id = str(uuid4())

    result = await _call(chunk_ids=[chunk_id, chunk_id])

    assert result.data == _Cited(cited=1)


async def test_no_passage_returns_a_recovery_hint() -> None:
    result = await _call(chunk_ids=[])

    assert result.success is False
    assert result.recovery_hint
    assert "source" in result.recovery_hint


async def test_missing_identity_raises_tool_error() -> None:
    async with Client(knowledge_server()) as client:
        with pytest.raises(ToolError):
            await client.call_tool(CITE_SOURCES_TOOL, {"chunk_ids": [str(uuid4())]})


@pytest.mark.parametrize(
    "chunk_ids",
    [["not-a-uuid"], [str(uuid4()) for _ in range(MAX_CITED + 1)]],
    ids=["not_a_uuid", "too_many"],
)
async def test_bad_arguments_are_refused_by_the_schema(chunk_ids: list[str]) -> None:
    async with Client(knowledge_server()) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                CITE_SOURCES_TOOL,
                {"chunk_ids": chunk_ids},
                meta={USER_ID_META_KEY: str(_ATHLETE)},
            )


async def test_a_refused_call_leaves_the_run_recoverable() -> None:
    """`tool_error_behavior` stays at 'retry': the model sees the error and calls again."""
    good = str(uuid4())

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls = sum(
            isinstance(part, ToolCallPart)
            for message in messages
            if isinstance(message, ModelResponse)
            for part in message.parts
        )
        if calls == 0:
            return ModelResponse(parts=[ToolCallPart(CITE_SOURCES_TOOL, {"chunk_ids": ["x"]})])
        if calls == 1:
            return ModelResponse(parts=[ToolCallPart(CITE_SOURCES_TOOL, {"chunk_ids": [good]})])
        return ModelResponse(parts=[TextPart("Done.")])

    class _Deps:
        user_id = _ATHLETE

    agent: Agent[Any, str] = Agent(
        FunctionModel(model), toolsets=[build_toolset(knowledge_server())]
    )

    result = await agent.run("cite", deps=_Deps())

    assert result.output == "Done."
