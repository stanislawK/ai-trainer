"""Which sources a cited answer shows (ADR-0036 #428 amendment, PRD B8): the passages the model
named through `cite_sources` that a `search_knowledge` result of the same run returned, one row
per source and locator, in the order first cited."""

from typing import Any
from uuid import UUID, uuid4

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)

from ai_trainer.domain.knowledge import EvidenceTier, SourceLanguage, SourceType
from ai_trainer.domain.knowledge_document import CueLocator, PageLocator
from ai_trainer.domain.knowledge_search import CitedSource, KnowledgeHit, KnowledgeSearchPage
from ai_trainer.mcp.citations import cited_sources
from ai_trainer.mcp.results import ToolResult
from ai_trainer.mcp.server import CITE_SOURCES_TOOL, SEARCH_KNOWLEDGE_TOOL

_BOOK = uuid4()
_TALK = uuid4()


def _hit(
    *,
    source_id: UUID = _BOOK,
    title: str = "Periodization",
    author: str | None = "Tudor Bompa",
    page: int = 12,
) -> KnowledgeHit:
    return KnowledgeHit(
        chunk_id=uuid4(),
        text="Plan a lighter week every fourth to sixth week.",
        heading_path=("Deload",),
        language=SourceLanguage.EN,
        source_id=source_id,
        title=title,
        author=author,
        evidence_tier=EvidenceTier.COACHING_BOOK,
        source_type=SourceType.BOOK,
        source_url=None,
        attribution="Bompa, Periodization, CC BY 4.0",
        locator=PageLocator(page=page),
        exercise_id=None,
    )


def _searched(*hits: KnowledgeHit, call_id: str = "search-1") -> list[ModelMessage]:
    result = ToolResult[KnowledgeSearchPage].ok(KnowledgeSearchPage(hits=hits, truncated=False))
    return _returned(SEARCH_KNOWLEDGE_TOOL, {"query": "deload"}, result.model_dump(mode="json"), call_id)


def _failed_search(call_id: str = "search-1") -> list[ModelMessage]:
    result = ToolResult[KnowledgeSearchPage].failed("No passage matches.", "Say so.")
    return _returned(SEARCH_KNOWLEDGE_TOOL, {"query": "deload"}, result.model_dump(mode="json"), call_id)


def _cited(*chunk_ids: UUID | str, call_id: str = "cite-1") -> list[ModelMessage]:
    args = {"chunk_ids": [str(chunk_id) for chunk_id in chunk_ids]}
    return _returned(CITE_SOURCES_TOOL, args, {"success": True}, call_id)


def _returned(name: str, args: dict[str, Any], content: object, call_id: str) -> list[ModelMessage]:
    return [
        ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=call_id)]),
        ModelRequest(parts=[ToolReturnPart(name, content, tool_call_id=call_id)]),
    ]


_ANSWER: list[ModelMessage] = [ModelResponse(parts=[TextPart("A lighter week every 4–6 weeks.")])]


def test_cited_sources_are_the_searched_passages_the_reply_cited() -> None:
    used, unused = _hit(page=12), _hit(source_id=_TALK, title="Recovery talk", page=1)

    sources = cited_sources([*_searched(used, unused), *_cited(used.chunk_id), *_ANSWER])

    assert sources == (
        CitedSource(
            source_id=_BOOK,
            title="Periodization",
            author="Tudor Bompa",
            evidence_tier=EvidenceTier.COACHING_BOOK,
            source_type=SourceType.BOOK,
            attribution="Bompa, Periodization, CC BY 4.0",
            locator=PageLocator(page=12),
        ),
    )


def test_no_cite_call_means_no_sources() -> None:
    assert cited_sources([*_searched(_hit()), *_ANSWER]) == ()


def test_an_id_no_search_returned_is_dropped() -> None:
    found = _hit()

    sources = cited_sources([*_searched(found), *_cited(uuid4(), found.chunk_id), *_ANSWER])

    assert [source.title for source in sources] == ["Periodization"]


def test_a_failed_search_cites_nothing() -> None:
    assert cited_sources([*_failed_search(), *_cited(uuid4()), *_ANSWER]) == ()


def test_one_row_per_source_and_locator_in_the_order_first_cited() -> None:
    page_12, also_page_12, page_40 = _hit(page=12), _hit(page=12), _hit(page=40)
    talk = _hit(source_id=_TALK, title="Recovery talk", author=None)
    talk = talk.model_copy(update={"locator": CueLocator(start_seconds=65, end_seconds=90)})

    sources = cited_sources(
        [
            *_searched(page_12, also_page_12, page_40, talk),
            *_cited(talk.chunk_id, page_12.chunk_id, call_id="cite-1"),
            *_cited(also_page_12.chunk_id, page_40.chunk_id, talk.chunk_id, call_id="cite-2"),
            *_ANSWER,
        ]
    )

    assert [(source.title, source.locator) for source in sources] == [
        ("Recovery talk", CueLocator(start_seconds=65, end_seconds=90)),
        ("Periodization", PageLocator(page=12)),
        ("Periodization", PageLocator(page=40)),
    ]


def test_passages_from_every_search_of_the_run_can_be_cited() -> None:
    first, second = _hit(page=3), _hit(page=9)

    sources = cited_sources(
        [
            *_searched(first, call_id="search-1"),
            *_searched(second, call_id="search-2"),
            *_cited(second.chunk_id, first.chunk_id),
        ]
    )

    assert [source.locator for source in sources] == [PageLocator(page=9), PageLocator(page=3)]


def test_malformed_cite_arguments_cite_nothing() -> None:
    found = _hit()
    messages: list[ModelMessage] = [
        *_searched(found),
        ModelResponse(parts=[ToolCallPart(CITE_SOURCES_TOOL, {"chunk_ids": "all"})]),
        ModelResponse(parts=[ToolCallPart(CITE_SOURCES_TOOL, "{not json")]),
        *_cited("not-a-uuid"),
    ]

    assert cited_sources(messages) == ()


def test_a_return_that_is_not_a_search_result_is_ignored() -> None:
    found = _hit()
    messages: list[ModelMessage] = [
        *_returned(SEARCH_KNOWLEDGE_TOOL, {"query": "deload"}, "Unknown tool name", "search-1"),
        *_searched(found, call_id="search-2"),
        *_cited(found.chunk_id),
    ]

    assert [source.title for source in cited_sources(messages)] == ["Periodization"]
