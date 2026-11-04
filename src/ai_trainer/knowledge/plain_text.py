"""The plain-text reader (ADR-0036): paragraphs split on blank lines, with no headings."""

from ai_trainer.domain.knowledge_document import (
    Block,
    BlockKind,
    KnowledgeDocument,
    Unreadable,
    UnreadableReason,
)
from ai_trainer.knowledge.text_decoding import decode_text, squash


def read_plain_text(data: bytes) -> KnowledgeDocument | Unreadable:
    text = decode_text(data)
    if text is None:
        return Unreadable(reason=UnreadableReason.NOT_UTF8)
    paragraphs: list[str] = []
    lines: list[str] = []
    for line in [*text.split("\n"), ""]:
        if line.strip():
            lines.append(line)
        elif lines:
            paragraphs.append(squash(lines))
            lines = []
    if not paragraphs:
        return Unreadable(reason=UnreadableReason.NO_TEXT)
    return KnowledgeDocument(
        blocks=tuple(Block(kind=BlockKind.PARAGRAPH, text=p) for p in paragraphs)
    )
