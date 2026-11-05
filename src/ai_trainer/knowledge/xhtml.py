"""XHTML to ordered blocks (ADR-0036, #426): one EPUB spine item's markup read with lxml's HTML
parser into headings, paragraphs, lists, tables and captions, each with its heading path.

The HTML parser recovers sloppy markup and entities, and never resolves an entity declared in the
file, so a hostile DOCTYPE can't read the host's files; the network is off. A file with NUL bytes
is binary, and one lxml can't parse at all is empty: both give no blocks.
"""

from collections.abc import Iterable

from lxml import etree

from ai_trainer.domain.knowledge_document import BlockKind

_SKIP = frozenset({"script", "style", "nav", "head", "svg", "math", "template", "noscript"})
_HEADINGS = {f"h{level}": level for level in range(1, 7)}
_LISTS = frozenset({"ul", "ol", "dl"})
_LIST_ITEMS = frozenset({"li", "dt", "dd"})
_CELLS = frozenset({"td", "th"})
# Elements whose children are blocks of their own; anything else flows into the paragraph.
_CONTAINERS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "aside",
        "blockquote",
        "figure",
        "main",
        "header",
        "footer",
        "body",
        "pre",
        "address",
        "details",
        "summary",
        "center",
        "form",
        "fieldset",
    }
)
_BLOCKS = frozenset(_HEADINGS) | _LISTS | _CONTAINERS | {"table", "figcaption"}

# A block's kind, its text and its heading path.
type RawBlock = tuple[BlockKind, str, tuple[str, ...]]


def local_name(element: etree._Element) -> str:
    """An element's tag without its namespace; a comment or processing instruction has none."""
    tag = element.tag
    return str(etree.QName(tag).localname) if isinstance(tag, str) else ""


def squash(texts: Iterable[str]) -> str:
    return " ".join("".join(texts).split())


def parse_html(data: bytes) -> etree._Element | None:
    """The document's root, or None for an empty file."""
    parser = etree.HTMLParser(
        encoding="utf-8",
        recover=True,
        no_network=True,
        remove_comments=True,
        remove_pis=True,
        huge_tree=False,
    )
    return etree.fromstring(data, parser)


class _Walker:
    """One tree as ordered blocks with their heading paths."""

    def __init__(self) -> None:
        self.blocks: list[RawBlock] = []
        self._headings: list[tuple[int, str]] = []

    def visit(self, element: etree._Element) -> None:
        """The element's children in order: a block child ends the paragraph being gathered,
        and text and inline children (and tails) are gathered into the next one."""
        run = [element.text or ""]
        for child in element:
            tag = local_name(child)
            if tag in _SKIP or not tag:
                pass
            elif tag in _BLOCKS:
                self._flush(run)
                run = []
                self._block(child, tag)
            else:
                run.append("".join(child.itertext()))
            run.append(child.tail or "")
        self._flush(run)

    def _block(self, element: etree._Element, tag: str) -> None:
        if tag in _HEADINGS:
            self._heading(element, _HEADINGS[tag])
        elif tag in _LISTS:
            self._list(element)
        elif tag == "table":
            self._table(element)
        elif tag == "figcaption":
            self._emit(BlockKind.CAPTION, squash(element.itertext()))
        else:
            self.visit(element)

    def _flush(self, run: list[str]) -> None:
        self._emit(BlockKind.PARAGRAPH, squash(run))

    def _emit(self, kind: BlockKind, text: str) -> None:
        if text:
            self.blocks.append((kind, text, tuple(title for _, title in self._headings)))

    def _heading(self, element: etree._Element, level: int) -> None:
        text = squash(element.itertext())
        if not text:
            return
        while self._headings and self._headings[-1][0] >= level:
            self._headings.pop()
        self._headings.append((level, text))
        self._emit(BlockKind.HEADING, text)

    def _list(self, element: etree._Element) -> None:
        items = (squash(item.itertext()) for item in element if local_name(item) in _LIST_ITEMS)
        self._emit(BlockKind.LIST, "\n".join(item for item in items if item))

    def _table(self, element: etree._Element) -> None:
        rows = []
        for row in element.iter("tr"):
            cells = [squash(cell.itertext()) for cell in row if local_name(cell) in _CELLS]
            if any(cells):
                rows.append(" | ".join(cells))
        self._emit(BlockKind.TABLE, "\n".join(rows))
        caption = element.find("caption")
        if caption is not None:
            self._emit(BlockKind.CAPTION, squash(caption.itertext()))


def xhtml_blocks(data: bytes) -> list[RawBlock] | None:
    """The file's blocks, or None when it isn't markup."""
    if b"\x00" in data:
        return None
    try:
        root = parse_html(data)
        if root is None:
            return None
        body = root.find("body")
        walker = _Walker()
        walker.visit(root if body is None else body)
    except etree.LxmlError, ValueError, RecursionError:
        return None
    return walker.blocks
