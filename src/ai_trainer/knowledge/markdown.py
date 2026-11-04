"""The Markdown reader (ADR-0036): ATX headings into the heading path, paragraphs, lists and pipe
tables as blocks, standard library only.

Leading `---` front matter is dropped: it describes the file, and licence detection reads it
separately (ADR-0036's #454 amendment). A fenced code block is kept as a paragraph, and nothing
inside it is read as a heading.
"""

import re
from dataclasses import dataclass, field

from ai_trainer.domain.knowledge_document import (
    Block,
    BlockKind,
    KnowledgeDocument,
    SectionLocator,
    Unreadable,
    UnreadableReason,
)
from ai_trainer.knowledge.text_decoding import decode_text, squash

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(```|~~~)")
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+")
_FRONT_MATTER_END = ("---", "...")


@dataclass
class _Reader:
    lines: list[str]
    at: int = 0
    # (level, title), outermost first.
    headings: list[tuple[int, str]] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(title for _, title in self.headings)

    def emit(self, kind: BlockKind, text: str) -> None:
        path = self.path
        locator = SectionLocator(section=path[-1] if path else None)
        self.blocks.append(Block(kind=kind, text=text, heading_path=path, locator=locator))

    def take_while(self, keep: re.Pattern[str] | None, *, stop_blank: bool = True) -> list[str]:
        taken: list[str] = []
        while self.at < len(self.lines):
            line = self.lines[self.at]
            if stop_blank and not line.strip():
                break
            if keep is not None and not keep.match(line):
                break
            taken.append(line)
            self.at += 1
        return taken


def _without_front_matter(lines: list[str]) -> list[str]:
    if not lines or lines[0].strip() != "---":
        return lines
    for end, line in enumerate(lines[1:], start=1):
        if line.strip() in _FRONT_MATTER_END:
            return lines[end + 1 :]
    return lines


def _heading(reader: _Reader, match: re.Match[str]) -> None:
    level, title = len(match.group(1)), " ".join(match.group(2).split())
    while reader.headings and reader.headings[-1][0] >= level:
        reader.headings.pop()
    reader.headings.append((level, title))
    reader.emit(BlockKind.HEADING, title)
    reader.at += 1


def _fence(reader: _Reader, marker: str) -> None:
    reader.at += 1
    body: list[str] = []
    while reader.at < len(reader.lines):
        line = reader.lines[reader.at]
        reader.at += 1
        if line.lstrip().startswith(marker):
            break
        body.append(line)
    text = "\n".join(body).strip("\n")
    if text.strip():
        reader.emit(BlockKind.PARAGRAPH, text)


def _list(reader: _Reader) -> None:
    items = [reader.lines[reader.at]]
    reader.at += 1
    # Continuation lines belong to the list until a blank line.
    items.extend(reader.take_while(None))
    reader.emit(BlockKind.LIST, "\n".join(line.rstrip() for line in items))


def _table(reader: _Reader) -> None:
    rows = reader.take_while(re.compile(r"^[ \t]*\|"))
    reader.emit(BlockKind.TABLE, "\n".join(row.strip() for row in rows))


def _starts_block(line: str) -> bool:
    return bool(
        _HEADING.match(line)
        or _FENCE.match(line)
        or _LIST_ITEM.match(line)
        or line.lstrip().startswith("|")
    )


def _paragraph(reader: _Reader) -> None:
    lines = [reader.lines[reader.at]]
    reader.at += 1
    while reader.at < len(reader.lines):
        line = reader.lines[reader.at]
        if not line.strip() or _starts_block(line):
            break
        lines.append(line)
        reader.at += 1
    reader.emit(BlockKind.PARAGRAPH, squash(lines))


def _step(reader: _Reader) -> None:
    line = reader.lines[reader.at]
    if not line.strip():
        reader.at += 1
    elif heading := _HEADING.match(line):
        _heading(reader, heading)
    elif fence := _FENCE.match(line):
        _fence(reader, fence.group(1))
    elif _LIST_ITEM.match(line):
        _list(reader)
    elif line.lstrip().startswith("|"):
        _table(reader)
    else:
        _paragraph(reader)


def read_markdown(data: bytes) -> KnowledgeDocument | Unreadable:
    text = decode_text(data)
    if text is None:
        return Unreadable(reason=UnreadableReason.NOT_UTF8)
    reader = _Reader(lines=_without_front_matter(text.split("\n")))
    while reader.at < len(reader.lines):
        _step(reader)
    if all(block.kind is BlockKind.HEADING for block in reader.blocks):
        return Unreadable(reason=UnreadableReason.NO_TEXT)
    return KnowledgeDocument(blocks=tuple(reader.blocks))
