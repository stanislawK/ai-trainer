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
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from ai_trainer.application.knowledge_search import KnowledgeSearch
from ai_trainer.application.training_history import TrainingHistory
from ai_trainer.domain.embeddings import (
    Embedded,
    EmbeddingFailed,
    EmbeddingFailure,
    EmbeddingOutcome,
    RateLimited,
)
from ai_trainer.domain.knowledge import EvidenceTier, SourceLanguage, SourceType
from ai_trainer.domain.knowledge_document import PageLocator
from ai_trainer.domain.knowledge_search import (
    KnowledgeCandidates,
    KnowledgeFilters,
    KnowledgeHit,
    KnowledgeSearchPage,
)
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.mcp.identity import USER_ID_META_KEY
from ai_trainer.mcp.results import ToolResult
from ai_trainer.mcp.server import SEARCH_KNOWLEDGE_TOOL, build_mcp_server
from ai_trainer.mcp.toolset import build_toolset
from tests.mcp_support import no_exercise_tools

_ATHLETE = UUID("00000000-0000-4000-8000-000000000424")
_MODEL = "qwen/qwen3-embedding-8b"

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


class _Embeddings:
    def __init__(self, outcome: EmbeddingOutcome | None = None) -> None:
        self.outcome = outcome or Embedded(vectors=((1.0, 0.0),), model=_MODEL)
        self.acting: list[UUID] = []

    async def embed_query(self, text: str, *, acting_user_id: UUID) -> EmbeddingOutcome:
        self.acting.append(acting_user_id)
        return self.outcome


class _Repository:
    def __init__(self, hits: Sequence[KnowledgeHit] = ()) -> None:
        self.hits = tuple(hits)

    async def candidates(
        self, *, vector: Sequence[float], query: str, filters: KnowledgeFilters, depth: int
    ) -> KnowledgeCandidates:
        return KnowledgeCandidates(by_meaning=self.hits, by_words=())


class _NoSessions:
    async def list_sessions(self, *args: Any, **kwargs: Any) -> Sequence[Any]:
        return []


def _server(embeddings: _Embeddings | None = None, hits: Sequence[KnowledgeHit] = (_HIT,)) -> Any:
    registry = default_sport_registry()
    return build_mcp_server(
        TrainingHistory(_NoSessions(), registry),
        *no_exercise_tools(),
        knowledge=KnowledgeSearch(
            embeddings=embeddings or _Embeddings(),
            repository=_Repository(hits),
            sports=registry,
            embedding_model=_MODEL,
            depth=20,
        ),
    )


async def _call(server: Any, **args: Any) -> ToolResult[KnowledgeSearchPage]:
    async with Client(server) as client:
        result = await client.call_tool(
            SEARCH_KNOWLEDGE_TOOL, args, meta={USER_ID_META_KEY: str(_ATHLETE)}
        )
    return ToolResult[KnowledgeSearchPage].model_validate(result.structured_content)


async def test_passages_come_back_with_their_source_and_locator() -> None:
    embeddings = _Embeddings()

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
    result = await _call(_server(_Embeddings(outcome)), query="how often to deload")

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


async def test_the_specialist_toolset_leaves_search_knowledge_out() -> None:
    """Held back from `gym.answer` until #428 wires it with `/tune-prompt` (ADR-0036 #424)."""
    model = TestModel(call_tools=[])
    agent: Agent[Any, str] = Agent(model, toolsets=[build_toolset(_server())])

    class _Deps:
        user_id = _ATHLETE

    await agent.run("hi", deps=_Deps())

    assert model.last_model_request_parameters is not None
    names = {tool.name for tool in model.last_model_request_parameters.function_tools}
    assert SEARCH_KNOWLEDGE_TOOL not in names
    assert {"query_sessions", "find_exercise", "exercise_history"} <= names
