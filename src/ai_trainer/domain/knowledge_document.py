"""The one internal document model every reader builds, and the chunks the chunker cuts from it
(ADR-0036, research knowledge-ingestion-at-scale.md rec 8, knowledge-base.md rec 7).

A reader turns a file into ordered blocks, each with its heading path and a locator. The chunker
walks those blocks, whatever the file was. A locator is data; the citation string is built when
an answer cites it.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# ADR-0033: `qwen/qwen3-embedding-8b` asked for 1,024 dimensions, stored as `vector(1024)`.
EMBEDDING_DIMENSIONS = 1024


class BlockKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    CAPTION = "caption"
    CUE = "cue"


class SectionKind(StrEnum):
    """What part of a source a chunk is: the abstract is its own chunk, and a table stays whole
    with its caption (ADR-0036)."""

    ABSTRACT = "abstract"
    BODY = "body"
    TABLE = "table"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SectionLocator(_Frozen):
    """Markdown, text and JATS: the nearest section title, if the source has one."""

    kind: Literal["section"] = "section"
    section: str | None = None


class PageLocator(_Frozen):
    """PDF: the 1-based page, and its printed label when that differs (#425)."""

    kind: Literal["page"] = "page"
    page: Annotated[int, Field(ge=1)]
    page_label: str | None = None


class SpineLocator(_Frozen):
    """EPUB: the spine item the passage is in (#426)."""

    kind: Literal["spine"] = "spine"
    spine_item: str


class CueLocator(_Frozen):
    """VTT and SRT: the first and last cue's times, in seconds (#426)."""

    kind: Literal["cue"] = "cue"
    start_seconds: Annotated[float, Field(ge=0)]
    end_seconds: Annotated[float, Field(ge=0)]


type Locator = Annotated[
    SectionLocator | PageLocator | SpineLocator | CueLocator, Field(discriminator="kind")
]


class Block(_Frozen):
    kind: BlockKind
    text: str
    # The headings above this block, outermost first; a heading's own path includes itself.
    heading_path: tuple[str, ...] = ()
    locator: Locator = SectionLocator()


class KnowledgeDocument(_Frozen):
    blocks: tuple[Block, ...]


class UnreadableReason(StrEnum):
    NOT_UTF8 = "not_utf8"
    NO_TEXT = "no_text"


class Unreadable(_Frozen):
    """A file its reader can't turn into a document, and why."""

    reason: UnreadableReason


class NewKnowledgeChunk(_Frozen):
    ordinal: Annotated[int, Field(ge=0)]
    text: str
    heading_path: tuple[str, ...]
    section_kind: SectionKind
    locator: Locator


def embedding_input(title: str, chunk: NewKnowledgeChunk) -> str:
    """What is embedded for a chunk: `title — heading path`, then its text (ADR-0036). A
    document carries no instruction; only a query does (ADR-0033)."""
    heading = " > ".join(chunk.heading_path)
    lead = f"{title} — {heading}" if heading else title
    return f"{lead}\n\n{chunk.text}"


def word_count(text: str) -> int:
    return len(text.split())
