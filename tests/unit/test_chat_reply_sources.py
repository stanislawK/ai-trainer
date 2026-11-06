"""Sources under a cited reply (PRD B8, ADR-0036 #428, the Reply Sources mock from #419): one
source shows in full as a row; several collapse behind an "N sources" chip. A row is the title
above "author · tier · locator", a missing part left out with its separator, and the licence's
attribution under it when the source has one. Rows aren't links. Rendered from the saved reply,
so a reload shows what the stream's end showed."""

import html
import re
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.knowledge import EvidenceTier, SourceType
from ai_trainer.domain.knowledge_document import (
    CueLocator,
    Locator,
    PageLocator,
    SectionLocator,
    SpineLocator,
)
from ai_trainer.domain.knowledge_search import CitedSource
from ai_trainer.web.templating import build_templates
from tests.chat_support import FROZEN_NOW, InMemoryChatRepository, include_chat, sign_in_as

_SOURCES = re.compile(r'<ul [^>]*aria-label="Sources"[^>]*>(.*?)</ul>', re.S)
_ROW = re.compile(r"<li [^>]*>(.*?)</li>", re.S)
_TAGS = re.compile(r"<[^>]+>")


def _source(
    title: str = "Periodization Training for Sports",
    *,
    author: str | None = "Tudor Bompa",
    tier: EvidenceTier = EvidenceTier.COACHING_BOOK,
    locator: Locator | None = None,
    attribution: str | None = None,
) -> CitedSource:
    return CitedSource(
        source_id=uuid4(),
        title=title,
        author=author,
        evidence_tier=tier,
        source_type=SourceType.BOOK,
        attribution=attribution,
        locator=locator or PageLocator(page=212),
    )


def _client() -> tuple[TestClient, UUID, InMemoryChatRepository]:
    app = FastAPI()
    chat = include_chat(app, build_templates())
    return TestClient(app), sign_in_as(app).id, chat


async def _page_with(*sources: CitedSource) -> str:
    client, user_id, chat = _client()
    conversation = await chat.conversation_of(user_id)
    await chat.add(user_id, conversation, ChatRole.USER, "How often should I deload?", FROZEN_NOW)
    await chat.add(
        user_id,
        conversation,
        ChatRole.ASSISTANT,
        "Bompa suggests a lighter week every 4-6 weeks.",
        FROZEN_NOW,
        sports=("gym",),
        template_id="gym.answer",
        template_version=2,
        sources=sources,
    )
    return client.get("/").text


def _rows(page: str) -> list[list[str]]:
    """Each source row's lines, as text."""
    (listed,) = _SOURCES.findall(page)
    return [
        [
            html.unescape(" ".join(_TAGS.sub(" ", line).split()))
            for line in re.split(r"</span>", row)
            if _TAGS.sub("", line).strip()
        ]
        for row in _ROW.findall(listed)
    ]


async def test_reply_shows_title_author_tier_and_locator() -> None:
    page = await _page_with(_source())

    assert _rows(page) == [
        ["Periodization Training for Sports", "Tudor Bompa · Coaching book · p. 212"]
    ]
    assert "1 source" not in page  # one source shows in full, with no chip
    assert "<details" not in page.split("Bompa suggests", 1)[1]


async def test_several_sources_collapse_behind_a_count() -> None:
    page = await _page_with(
        _source(),
        _source("Deload timing", author="M. Kowalska et al.", tier=EvidenceTier.PEER_REVIEWED),
        _source("Coaches' roundtable", tier=EvidenceTier.ARTICLE_OR_TRANSCRIPT),
    )

    reply = page.split("Bompa suggests", 1)[1]
    assert re.search(r"<details[^>]*>\s*<summary[^>]*lg-chip[^>]*>.*?3 sources", reply, re.S)
    assert [row[0] for row in _rows(page)] == [
        "Periodization Training for Sports",
        "Deload timing",
        "Coaches' roundtable",
    ]


async def test_a_missing_author_drops_out_with_its_separator() -> None:
    page = await _page_with(_source(author=None))

    assert _rows(page)[0][1] == "Coaching book · p. 212"


async def test_each_locator_kind_reads_as_the_mock_writes_it() -> None:
    page = await _page_with(
        _source("A", locator=PageLocator(page=7, page_label="vii")),
        _source("B", locator=SpineLocator(spine_item="ch04.xhtml", chapter="Recovery weeks")),
        _source("C", locator=CueLocator(start_seconds=760, end_seconds=800)),
        _source("D", locator=CueLocator(start_seconds=3725, end_seconds=3800)),
        _source("E", locator=SectionLocator(section="Discussion", doi="10.1/x")),
        _source("F", locator=SectionLocator()),
        _source("G", locator=SpineLocator(spine_item="ch05.xhtml")),
        _source("H", locator=SectionLocator(doi="10.1186/s40798-024-00700-1")),
    )

    assert [row[1] for row in _rows(page)] == [
        "Tudor Bompa · Coaching book · p. vii",
        "Tudor Bompa · Coaching book · Recovery weeks",
        "Tudor Bompa · Coaching book · 12:40",
        "Tudor Bompa · Coaching book · 1:02:05",
        "Tudor Bompa · Coaching book · Discussion",
        "Tudor Bompa · Coaching book",
        "Tudor Bompa · Coaching book",
        "Tudor Bompa · Coaching book · doi:10.1186/s40798-024-00700-1",
    ]


async def test_a_licence_attribution_shows_under_its_row() -> None:
    page = await _page_with(_source(attribution="Bompa, Periodization, CC BY 4.0"))

    assert _rows(page)[0][2] == "Bompa, Periodization, CC BY 4.0"


async def test_rows_are_not_links() -> None:
    (listed,) = _SOURCES.findall(await _page_with(_source(), _source("Other")))

    assert "<a " not in listed


async def test_a_reply_with_no_sources_shows_none() -> None:
    page = await _page_with()

    assert not _SOURCES.findall(page)
    assert "sources" not in page.split("Bompa suggests", 1)[1].split("</div>", 1)[0]


async def test_source_text_is_escaped() -> None:
    page = await _page_with(_source("<script>alert(1)</script>"))

    assert "<script>alert(1)" not in page
    assert "&lt;script&gt;" in page
