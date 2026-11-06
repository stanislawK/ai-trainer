"""`search_knowledge` through FastMCP's in-memory `Client(server)` (ADR-0010, ADR-0033, #424):
the envelope on success, every domain failure as `success=False` with a `recovery_hint`, and a
missing identity as `ToolError`. The search's legs are faked at their ports here; they run on
PostgreSQL in `test_knowledge_search.py`.

Chunks are shared reference data with no owner, so there is no tenancy case: the caller's
identity is only the embedding call's acting user, which the first test pins."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from ai_trainer.domain.embeddings import (
    Embedded,
    EmbeddingFailed,
    EmbeddingFailure,
    EmbeddingOutcome,
    RateLimited,
)
from ai_trainer.domain.knowledge import EvidenceTier, SourceLanguage, SourceType
from ai_trainer.domain.knowledge_document import PageLocator
from ai_trainer.domain.knowledge_search import KnowledgeHit, KnowledgeSearchPage
from ai_trainer.mcp.identity import USER_ID_META_KEY
from ai_trainer.mcp.results import ToolResult
from ai_trainer.mcp.server import CITE_SOURCES_TOOL, SEARCH_KNOWLEDGE_TOOL
from ai_trainer.mcp.toolset import build_toolset
from tests.mcp_support import QueryEmbeddings, knowledge_server

_ATHLETE = UUID("00000000-0000-4000-8000-000000000424")

_HIT = KnowledgeHit(
    chunk_id=uuid4(),
    text="Deload every fourth week.",
    heading_path=("Recovery",),
    language=SourceLanguage.EN,
    source_id=uuid4(),
    title="Periodization",
    author="T. Bompa",
    evidence_tier=EvidenceTier.COACHING_BOOK,
    source_type=SourceType.BOOK,
    source_url=None,
    attribution="T. Bompa, Periodization, CC BY 4.0",
    locator=PageLocator(page=12),
    exercise_id=None,
)


def _server(
    embeddings: QueryEmbeddings | None = None, hits: Sequence[KnowledgeHit] = (_HIT,)
) -> FastMCP:
    return knowledge_server(hits, embeddings)


async def _call(server: Any, **args: Any) -> ToolResult[KnowledgeSearchPage]:
    async with Client(server) as client:
        result = await client.call_tool(
            SEARCH_KNOWLEDGE_TOOL, args, meta={USER_ID_META_KEY: str(_ATHLETE)}
        )
    return ToolResult[KnowledgeSearchPage].model_validate(result.structured_content)


async def test_passages_come_back_with_their_source_and_locator() -> None:
    embeddings = QueryEmbeddings()

    result = await _call(_server(embeddings), query="how often to deload", sport="gym")

    assert result == ToolResult[KnowledgeSearchPage](
        success=True, data=KnowledgeSearchPage(hits=(_HIT,), truncated=False)
    )
    assert embeddings.acting == [_ATHLETE]


async def test_no_match_returns_a_recovery_hint() -> None:
    result = await _call(_server(hits=()), query="how often to deload")

    assert result.success is False
    assert result.data is None
    assert result.error
    assert result.recovery_hint


@pytest.mark.parametrize(
    "outcome",
    [
        EmbeddingFailed(reason=EmbeddingFailure.TIMEOUT),
        EmbeddingFailed(reason=EmbeddingFailure.ERROR),
        EmbeddingFailed(reason=EmbeddingFailure.WRONG_SHAPE),
        RateLimited(retry_after_seconds=3),
    ],
    ids=["timeout", "error", "wrong_shape", "rate_limited"],
)
async def test_a_failed_embedding_call_returns_a_recovery_hint(outcome: EmbeddingOutcome) -> None:
    result = await _call(_server(QueryEmbeddings(outcome)), query="how often to deload")

    assert result.success is False
    assert result.recovery_hint
    assert "source" in result.recovery_hint


async def test_a_blank_query_returns_a_recovery_hint() -> None:
    result = await _call(_server(), query="   ")
    assert (result.success, bool(result.recovery_hint)) == (False, True)


async def test_an_unknown_sport_names_the_known_ones() -> None:
    result = await _call(_server(), query="deload", sport="chess")

    assert result.success is False
    assert result.recovery_hint is not None
    assert "gym" in result.recovery_hint


async def test_missing_identity_raises_tool_error() -> None:
    async with Client(_server()) as client:
        with pytest.raises(ToolError):
            await client.call_tool(SEARCH_KNOWLEDGE_TOOL, {"query": "deload"})


async def test_a_limit_out_of_bounds_is_refused_by_the_schema() -> None:
    async with Client(_server()) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                SEARCH_KNOWLEDGE_TOOL,
                {"query": "deload", "limit": 11},
                meta={USER_ID_META_KEY: str(_ATHLETE)},
            )


async def test_the_specialist_is_offered_only_read_only_tools() -> None:
    """`gym.answer` sees `search_knowledge` and `cite_sources` from #428 on, and nothing that
    writes, so a passage that gives orders has nothing to act through (ADR-0036 #428)."""
    model = TestModel(call_tools=[])
    agent: Agent[Any, str] = Agent(model, toolsets=[build_toolset(_server())])

    class _Deps:
        user_id = _ATHLETE

    await agent.run("hi", deps=_Deps())

    assert model.last_model_request_parameters is not None
    names = {tool.name for tool in model.last_model_request_parameters.function_tools}
    assert names == {
        "query_sessions",
        "find_exercise",
        "exercise_history",
        SEARCH_KNOWLEDGE_TOOL,
        CITE_SOURCES_TOOL,
    }
