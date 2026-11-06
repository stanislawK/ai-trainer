"""Which sources a cited answer shows (PRD B8, ADR-0036 #428 amendment), read from a finished
tool loop's messages: the passages the model named through `cite_sources` that a successful
`search_knowledge` result of the same run returned. A passage can't make the reply cite what no
search retrieved, and an invented ID is dropped. No `cite_sources` call, no sources.

`main.py` hands this to the gateway with the toolset, since `llm` and `mcp` don't import each
other (ADR-0003)."""

from collections.abc import Iterator, Sequence
from uuid import UUID

from pydantic import BaseModel, ValidationError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.messages import ToolReturnPart

from ai_trainer.domain.knowledge_search import CitedSource, KnowledgeHit, KnowledgeSearchPage
from ai_trainer.mcp.results import ToolResult
from ai_trainer.mcp.server import CITE_SOURCES_TOOL, SEARCH_KNOWLEDGE_TOOL


class _CiteArgs(BaseModel):
    chunk_ids: list[UUID]


def cited_sources(messages: Sequence[ModelMessage]) -> tuple[CitedSource, ...]:
    """One row per source and locator, in the order first cited."""
    found = {hit.chunk_id: hit for hit in _searched(messages)}
    rows: dict[tuple[UUID, object], CitedSource] = {}
    for chunk_id in _cited_ids(messages):
        hit = found.get(chunk_id)
        if hit is not None:
            rows.setdefault((hit.source_id, hit.locator), CitedSource.of(hit))
    return tuple(rows.values())


def _searched(messages: Sequence[ModelMessage]) -> Iterator[KnowledgeHit]:
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, ToolReturnPart) and part.tool_name == SEARCH_KNOWLEDGE_TOOL:
                try:
                    result = ToolResult[KnowledgeSearchPage].model_validate(part.content)
                except ValidationError:
                    continue
                if result.data is not None:
                    yield from result.data.hits


def _cited_ids(messages: Sequence[ModelMessage]) -> Iterator[UUID]:
    for message in messages:
        if not isinstance(message, ModelResponse):
            continue
        for part in message.parts:
            if isinstance(part, ToolCallPart) and part.tool_name == CITE_SOURCES_TOOL:
                try:
                    yield from _CiteArgs.model_validate(part.args_as_dict()).chunk_ids
                except ValidationError, ValueError:
                    continue
