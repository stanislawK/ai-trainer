"""The PDF reader (ADR-0036): paragraphs within a page, each block's locator the 1-based page and
its printed label when that differs. Running headers, footers and page numbers are dropped.

A PDF has no headings the reader can trust, so blocks carry an empty heading path. pypdf, a
pure-Python BSD-3-Clause library, extracts the text; there is no OCR, so a scan is reported as
having no text layer instead of being ingested empty.
"""

import re
from collections import Counter
from collections.abc import Sequence
from io import BytesIO

from pypdf import PdfReader

from ai_trainer.domain.knowledge_document import (
    Block,
    BlockKind,
    KnowledgeDocument,
    PageLocator,
    Unreadable,
    UnreadableReason,
)

# A running header or footer is a short line at a page's edge that repeats, digits aside, on at
# least this share of the pages; it needs a few pages to be told apart from real text.
_MIN_PAGES_FOR_HEADERS = 3
_HEADER_SHARE = 0.5
_EDGE_LINES = 2
_HEADER_MAX_WORDS = 8
# A line that ends a sentence and fills under this share of the page's longest line ends a
# paragraph (pypdf doesn't mark blank lines).
_SHORT_LINE_SHARE = 0.6
_SENTENCE_END = (".", "!", "?", "…")
# A page number as a line of its own: `12`, `Page 12`, `12 / 40`, `- 12 -`, after digits are
# replaced by `#`.
_PAGE_NUMBER = re.compile(r"^\W*(page\s+)?#(\s*(of|/)\s*#)?\W*$")


def _shape(line: str) -> str:
    return re.sub(r"\d+", "#", " ".join(line.casefold().split()))


def _edge_shapes(lines: Sequence[str]) -> set[str]:
    edges = [*lines[:_EDGE_LINES], *lines[-_EDGE_LINES:]]
    return {_shape(line) for line in edges if len(line.split()) <= _HEADER_MAX_WORDS}


def _running_shapes(pages: Sequence[Sequence[str]]) -> set[str]:
    """The line shapes repeating at the edges of enough pages to be headers or footers."""
    if len(pages) < _MIN_PAGES_FOR_HEADERS:
        return set()
    seen = Counter(shape for lines in pages for shape in _edge_shapes(lines))
    return {shape for shape, count in seen.items() if count >= len(pages) * _HEADER_SHARE}


def _body_lines(lines: Sequence[str], running: set[str]) -> list[str]:
    edge = {
        *range(min(_EDGE_LINES, len(lines))),
        *range(max(len(lines) - _EDGE_LINES, 0), len(lines)),
    }
    kept: list[str] = []
    for at, line in enumerate(lines):
        shape = _shape(line)
        if at in edge and (shape in running or _PAGE_NUMBER.match(shape)):
            continue
        kept.append(line)
    return kept


def _paragraphs(lines: Sequence[str]) -> list[str]:
    longest = max((len(line) for line in lines), default=0)
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(line)
        if line.endswith(_SENTENCE_END) and len(line) < longest * _SHORT_LINE_SHARE:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def _page_lines(text: str) -> list[str]:
    """A page's text as lines with their whitespace squashed; a blank line stays as ''."""
    return [" ".join(line.split()) for line in text.splitlines()]


def _locator(page: int, labels: Sequence[str]) -> PageLocator:
    label = labels[page - 1] if page <= len(labels) else None
    return PageLocator(page=page, page_label=label if label and label != str(page) else None)


def _extract(data: bytes) -> tuple[list[str], list[str]] | Unreadable:
    """Each page's text, and the page labels. Anything pypdf can't parse is a corrupt file: it
    raises a family of errors, and a damaged file may also raise a plain `KeyError` or
    `RecursionError` from deep inside it."""
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            return Unreadable(reason=UnreadableReason.ENCRYPTED)
        if not reader.pages:
            return Unreadable(reason=UnreadableReason.CORRUPT)
        return [page.extract_text() for page in reader.pages], list(reader.page_labels)
    except Exception:
        return Unreadable(reason=UnreadableReason.CORRUPT)


def read_pdf(data: bytes) -> KnowledgeDocument | Unreadable:
    extracted = _extract(data)
    if isinstance(extracted, Unreadable):
        return extracted
    texts, labels = extracted
    pages = [[line for line in _page_lines(text) if line or text.strip()] for text in texts]
    running = _running_shapes([[line for line in lines if line] for lines in pages])
    blocks: list[Block] = []
    for number, lines in enumerate(pages, start=1):
        body = _body_lines(lines, running)
        blocks.extend(
            Block(
                kind=BlockKind.PARAGRAPH,
                text=paragraph,
                locator=_locator(number, labels),
            )
            for paragraph in _paragraphs(body)
            if paragraph
        )
    if blocks:
        return KnowledgeDocument(blocks=tuple(blocks))
    has_text = any(text.strip() for text in texts)
    reason = UnreadableReason.NO_TEXT if has_text else UnreadableReason.NO_TEXT_LAYER
    return Unreadable(reason=reason)
