"""The plain-text reader: paragraphs split on blank lines, no headings (#423), and the reader
registry that picks a reader by file type."""

import pytest

from ai_trainer.domain.knowledge import KnowledgeFileType
from ai_trainer.domain.knowledge_document import (
    BlockKind,
    KnowledgeDocument,
    NoReader,
    Unreadable,
    UnreadableReason,
)
from ai_trainer.knowledge.plain_text import read_plain_text
from ai_trainer.knowledge.readers import DocumentReaders


def test_paragraphs_split_on_blank_lines_with_their_lines_joined() -> None:
    document = read_plain_text(b"Rest  matters\nfor strength.\n\n\n   \nSleep too.\n")

    assert isinstance(document, KnowledgeDocument)
    assert [(b.kind, b.text, b.heading_path) for b in document.blocks] == [
        (BlockKind.PARAGRAPH, "Rest matters for strength.", ()),
        (BlockKind.PARAGRAPH, "Sleep too.", ()),
    ]


def test_blank_text_has_no_text() -> None:
    assert read_plain_text(b" \n\n\t\n") == Unreadable(reason=UnreadableReason.NO_TEXT)


@pytest.mark.parametrize("data", [b"\xc3\x28", b"text\x00more"], ids=["invalid", "nul"])
def test_bytes_that_are_not_text_are_unreadable(data: bytes) -> None:
    assert read_plain_text(data) == Unreadable(reason=UnreadableReason.NOT_UTF8)


@pytest.mark.parametrize(
    ("file_type", "kind"),
    [
        (KnowledgeFileType.MARKDOWN, BlockKind.HEADING),
        (KnowledgeFileType.TEXT, BlockKind.PARAGRAPH),
    ],
    ids=["markdown", "text"],
)
def test_the_registry_reads_markdown_and_text(
    file_type: KnowledgeFileType, kind: BlockKind
) -> None:
    document = DocumentReaders().parse(file_type, b"# Deload\n\nText.\n")

    assert isinstance(document, KnowledgeDocument)
    assert document.blocks[0].kind is kind


@pytest.mark.parametrize(
    "file_type",
    [KnowledgeFileType.JATS],
    ids=["jats"],
)
def test_a_type_with_no_reader_yet_says_so(file_type: KnowledgeFileType) -> None:
    assert DocumentReaders().parse(file_type, b"anything") == NoReader(file_type=file_type)
