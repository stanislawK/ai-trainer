"""The JATS reader (ADR-0036, #463): a PMC article's XML read with lxml into the shared document
model.

- The abstract is read first, under one `Abstract` heading, so the chunker makes it one chunk. A
  structured abstract's sub-section titles become lead-ins ("Background: …") instead of
  headings. Only the main abstract is read: a graphical or other typed abstract, and a
  translated one, are left out.
- The body is walked in order. A `<sec>` with a title is a heading and extends the heading path;
  one without a title keeps its parent's. A table becomes a `TABLE` block followed by its label
  and caption, which the chunker keeps whole. A list and a figure caption are blocks too.
- PMC keeps tables and figures in `<floats-group>`, outside the body. Each is read where a
  paragraph first cites it with an `<xref>`, so it lands in that section; one nothing cites comes
  last, under no section.
- `<back>` is never read, so the reference list, acknowledgements and funding are not embedded.
  Formulas, MathML and graphics add no text.
- A block's locator is its nearest section title and the article's DOI, the only thing to cite
  in a source without pages.
- The XML is parsed strictly with entities, DTDs and the network off, so a hostile DOCTYPE reads
  no file and expands nothing. A file that does not parse, or is not an `<article>`, is
  `CORRUPT`; one with no text to embed is `NO_TEXT`.
"""

from collections.abc import Callable, Collection, Mapping

from lxml import etree

from ai_trainer.domain.knowledge_document import (
    Block,
    BlockKind,
    KnowledgeDocument,
    SectionLocator,
    Unreadable,
    UnreadableReason,
)
from ai_trainer.domain.pmc import bare_doi
from ai_trainer.knowledge.xhtml import local_name, squash

_ABSTRACT = "Abstract"
# Elements with no text worth embedding: markup for maths and pictures.
_NO_TEXT = frozenset(
    {"math", "tex-math", "inline-formula", "disp-formula", "graphic", "inline-graphic", "media"}
)
# Elements a body walk never reads: their text is handled by their parent, or is not content
# (references, notes, funding and the like).
_SKIPPED = _NO_TEXT | frozenset(
    {
        "title",
        "label",
        "sec-meta",
        "object-id",
        "permissions",
        "table-wrap-foot",
        "ref-list",
        "fn-group",
        "ack",
        "app-group",
        "notes",
    }
)
# Elements that end a word where they end, as in `xhtml.py`.
_SEPARATED = frozenset({"p", "title", "label", "list-item", "td", "th", "tr", "break", "caption"})
# Blocks a paragraph may hold; they are read after its text.
_NESTED_BLOCKS = frozenset({"table-wrap", "fig"})
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
# Abstract types that are the article's main abstract.
_MAIN_ABSTRACTS = frozenset({None, "structured"})


def _xml(data: bytes) -> etree._Element:
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_comments=True,
        remove_pis=True,
    )
    return etree.fromstring(data, parser)


def _gather(element: etree._Element, parts: list[str], skip: Collection[str]) -> None:
    parts.append(element.text or "")
    for child in element:
        name = local_name(child)
        # An entity reference or other non-element node has no name; only its tail is text.
        if name and name not in skip:
            separated = name in _SEPARATED
            parts.append(" " if separated else "")
            _gather(child, parts, skip)
            parts.append(" " if separated else "")
        parts.append(child.tail or "")


def _text(element: etree._Element, *, skip: Collection[str] = _NO_TEXT) -> str:
    parts: list[str] = []
    _gather(element, parts, skip)
    return squash(parts)


def _child(element: etree._Element, name: str) -> etree._Element | None:
    return next((c for c in element if local_name(c) == name), None)


def _doi(article: etree._Element) -> str | None:
    for element in article.iter():
        if local_name(element) == "article-id" and element.get("pub-id-type") == "doi":
            return bare_doi(_text(element))
    return None


def _main_abstract(article: etree._Element) -> etree._Element | None:
    meta = next((e for e in article.iter() if local_name(e) == "article-meta"), None)
    if meta is None:
        return None
    language = article.get(_XML_LANG)
    return next(
        (
            e
            for e in meta
            if local_name(e) == "abstract"
            and e.get("abstract-type") in _MAIN_ABSTRACTS
            # One in another language than the article's own is a translation.
            and e.get(_XML_LANG) in (None, language)
        ),
        None,
    )


def _abstract_paragraphs(element: etree._Element) -> list[str]:
    """An abstract's paragraphs in order, a titled sub-section's first one led by its title."""
    paragraphs: list[str] = []
    for child in element:
        name = local_name(child)
        if name == "p":
            paragraphs.append(_text(child))
        elif name == "sec":
            inner = _abstract_paragraphs(child)
            title_element = _child(child, "title")
            title = _text(title_element) if title_element is not None else ""
            if title and inner:
                inner[0] = f"{title}: {inner[0]}"
            paragraphs.extend(inner)
    return [paragraph for paragraph in paragraphs if paragraph]


class _Walker:
    """One article as ordered blocks."""

    def __init__(self, doi: str | None) -> None:
        self.blocks: list[Block] = []
        self._doi = doi
        # Tables and figures PMC keeps outside the body, by id, until a paragraph cites them.
        self._floats: dict[str, etree._Element] = {}
        self._handlers: Mapping[str, Callable[[etree._Element, tuple[str, ...]], None]] = {
            "sec": self._section,
            "p": self._paragraph,
            "list": self._list,
            "table-wrap": self._table,
            "fig": self._figure,
        }

    def emit(self, kind: BlockKind, text: str, path: tuple[str, ...]) -> None:
        if text:
            locator = SectionLocator(section=path[-1] if path else None, doi=self._doi)
            self.blocks.append(Block(kind=kind, text=text, heading_path=path, locator=locator))

    def abstract(self, element: etree._Element) -> None:
        path = (_ABSTRACT,)
        paragraphs = _abstract_paragraphs(element)
        if paragraphs:
            self.emit(BlockKind.HEADING, _ABSTRACT, path)
        for paragraph in paragraphs:
            self.emit(BlockKind.PARAGRAPH, paragraph, path)

    def collect_floats(self, group: etree._Element) -> None:
        """PMC keeps an article's tables and figures in `<floats-group>`, after the body, and the
        body only cites them with an `<xref>`. They are read where first cited."""
        # `iter`, not the children: a `<table-wrap-group>` or `<fig-group>` wraps its members.
        for element in group.iter():
            if local_name(element) in _NESTED_BLOCKS:
                self._floats[element.get("id") or f"float-{len(self._floats)}"] = element

    def uncited_floats(self) -> None:
        """What no paragraph cited, in order, under no section."""
        for element in list(self._floats.values()):
            self._float(element, ())
        self._floats.clear()

    def _float(self, element: etree._Element, path: tuple[str, ...]) -> None:
        self._handlers[local_name(element)](element, path)

    def _cited_floats(self, paragraph: etree._Element, path: tuple[str, ...]) -> None:
        for xref in paragraph.iter():
            if local_name(xref) == "xref" and xref.get("ref-type") in ("table", "fig"):
                cited = self._floats.pop(xref.get("rid") or "", None)
                if cited is not None:
                    self._float(cited, path)

    def walk(self, parent: etree._Element, path: tuple[str, ...]) -> None:
        for child in parent:
            name = local_name(child)
            if not name or name in _SKIPPED:
                continue
            handler = self._handlers.get(name)
            if handler is None:
                self.walk(child, path)  # a box, a quotation, a definition list: its blocks
            else:
                handler(child, path)

    def _section(self, element: etree._Element, path: tuple[str, ...]) -> None:
        title_element = _child(element, "title")
        title = _text(title_element) if title_element is not None else ""
        if title:
            path = (*path, title)
            self.emit(BlockKind.HEADING, title, path)
        self.walk(element, path)

    def _paragraph(self, element: etree._Element, path: tuple[str, ...]) -> None:
        self.emit(BlockKind.PARAGRAPH, _text(element, skip=_NO_TEXT | _NESTED_BLOCKS), path)
        for child in element:
            if local_name(child) in _NESTED_BLOCKS:
                self._float(child, path)
        self._cited_floats(element, path)

    def _list(self, element: etree._Element, path: tuple[str, ...]) -> None:
        items = (_text(item) for item in element if local_name(item) == "list-item")
        self.emit(BlockKind.LIST, "\n".join(item for item in items if item), path)

    def _table(self, element: etree._Element, path: tuple[str, ...]) -> None:
        table = next((e for e in element.iter() if local_name(e) == "table"), None)
        if table is not None:
            rows = []
            for row in table.iter():
                if local_name(row) != "tr":
                    continue
                cells = [_text(cell) for cell in row if local_name(cell) in ("th", "td")]
                if any(cells):
                    rows.append(" | ".join(cells))
            self.emit(BlockKind.TABLE, "\n".join(rows), path)
        self.emit(BlockKind.CAPTION, _captioned(element), path)

    def _figure(self, element: etree._Element, path: tuple[str, ...]) -> None:
        self.emit(BlockKind.CAPTION, _captioned(element), path)


def _captioned(element: etree._Element) -> str:
    """A table's or figure's label and caption: `Table 1. Weekly volume by arm.`"""
    label_element = _child(element, "label")
    caption_element = _child(element, "caption")
    label = _text(label_element).rstrip(".") if label_element is not None else ""
    caption = _text(caption_element) if caption_element is not None else ""
    return f"{label}. {caption}" if label and caption else label or caption


def _read(data: bytes) -> KnowledgeDocument:
    article = _xml(data)
    if local_name(article) != "article":
        raise ValueError("not a JATS article")
    walker = _Walker(_doi(article))
    abstract = _main_abstract(article)
    if abstract is not None:
        walker.abstract(abstract)
    floats = next((e for e in article if local_name(e) == "floats-group"), None)
    if floats is not None:
        walker.collect_floats(floats)
    body = next((e for e in article if local_name(e) == "body"), None)
    if body is not None:
        walker.walk(body, ())
    walker.uncited_floats()
    if not any(block.kind is not BlockKind.HEADING for block in walker.blocks):
        raise _NoTextError
    return KnowledgeDocument(blocks=tuple(walker.blocks))


class _NoTextError(Exception):
    """An article with nothing to embed."""


def read_jats(data: bytes) -> KnowledgeDocument | Unreadable:
    try:
        return _read(data)
    except _NoTextError:
        return Unreadable(reason=UnreadableReason.NO_TEXT)
    except etree.LxmlError, RecursionError, ValueError:
        return Unreadable(reason=UnreadableReason.CORRUPT)
