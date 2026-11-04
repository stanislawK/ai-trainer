"""The PDF reader (#425, ADR-0036): paragraphs within a page, the page and its printed label as
the locator, running headers dropped, and a reason for a file it can't read."""

import pytest

from ai_trainer.domain.knowledge import KnowledgeFileType
from ai_trainer.domain.knowledge_document import (
    BlockKind,
    KnowledgeDocument,
    PageLocator,
    Unreadable,
    UnreadableReason,
)
from ai_trainer.knowledge.pdf import _paragraphs, read_pdf
from ai_trainer.knowledge.readers import DocumentReaders
from tests.pdf_support import blank_pdf, encrypted_pdf, text_pdf

_LONG = "Deload weeks let the body absorb the work of the hard blocks that came before them."


def _document(data: bytes) -> KnowledgeDocument:
    document = read_pdf(data)
    assert isinstance(document, KnowledgeDocument)
    return document


def test_paragraphs_carry_their_one_based_page() -> None:
    document = _document(text_pdf([[_LONG], ["Sleep matters too, said the coach to the athlete."]]))

    assert [(b.kind, b.locator) for b in document.blocks] == [
        (BlockKind.PARAGRAPH, PageLocator(page=1)),
        (BlockKind.PARAGRAPH, PageLocator(page=2)),
    ]
    assert document.blocks[0].text == _LONG
    assert all(b.heading_path == () for b in document.blocks)


def test_paragraphs_split_within_a_page() -> None:
    document = _document(
        text_pdf([[_LONG, _LONG, "Keep it short.", "A new paragraph starts here and goes on."]])
    )

    assert [b.text for b in document.blocks] == [
        f"{_LONG} {_LONG} Keep it short.",
        "A new paragraph starts here and goes on.",
    ]


def test_a_pdf_with_page_labels_records_both_numbers() -> None:
    data = text_pdf([[_LONG], [_LONG], [_LONG]], labels="0 << /S /r >> 2 << /S /D /St 5 >>")

    assert [b.locator for b in _document(data).blocks] == [
        PageLocator(page=1, page_label="i"),
        PageLocator(page=2, page_label="ii"),
        PageLocator(page=3, page_label="5"),
    ]


def test_a_label_equal_to_the_page_number_is_not_recorded() -> None:
    data = text_pdf([[_LONG], [_LONG]], labels="0 << /S /D >>")

    assert [b.locator for b in _document(data).blocks] == [
        PageLocator(page=1),
        PageLocator(page=2),
    ]


def test_running_headers_and_page_numbers_are_dropped() -> None:
    pages = [
        ["Strength Training Handbook", f"Chapter {n} body {_LONG}", f"Page {n}"]
        for n in range(1, 5)
    ]

    texts = [b.text for b in _document(text_pdf(pages)).blocks]

    assert texts == [f"Chapter {n} body {_LONG}" for n in range(1, 5)]


def test_a_line_seen_on_few_pages_is_kept() -> None:
    pages = [["Intro line", _LONG], [_LONG], [_LONG], [_LONG]]

    assert "Intro line" in _document(text_pdf(pages)).blocks[0].text


def test_a_pdf_with_no_text_layer_is_unreadable_as_a_scan() -> None:
    assert read_pdf(blank_pdf()) == Unreadable(reason=UnreadableReason.NO_TEXT_LAYER)


@pytest.mark.parametrize(
    "data",
    [b"%PDF-1.7\nnot really a pdf", b"%PDF-1.4\n" + b"\x00" * 64, text_pdf([[_LONG]])[:120]],
    ids=["garbage", "zeros", "truncated"],
)
def test_a_corrupt_pdf_is_unreadable(data: bytes) -> None:
    assert read_pdf(data) == Unreadable(reason=UnreadableReason.CORRUPT)


def test_a_pdf_with_no_pages_is_unreadable() -> None:
    assert read_pdf(blank_pdf(pages=0)) == Unreadable(reason=UnreadableReason.CORRUPT)


def test_an_encrypted_pdf_is_unreadable() -> None:
    data = encrypted_pdf(text_pdf([[_LONG]]), user_password="secret")

    assert read_pdf(data) == Unreadable(reason=UnreadableReason.ENCRYPTED)


def test_a_pdf_that_opens_with_an_empty_password_is_read() -> None:
    data = encrypted_pdf(text_pdf([[_LONG]]), user_password="", owner_password="owner")

    assert _document(data).blocks[0].text == _LONG


def test_the_registry_reads_pdf() -> None:
    document = DocumentReaders().parse(KnowledgeFileType.PDF, text_pdf([[_LONG]]))

    assert isinstance(document, KnowledgeDocument)


def test_a_blank_line_ends_a_paragraph() -> None:
    assert _paragraphs(["First part", "goes on.", "", "Second part.", ""]) == [
        "First part goes on.",
        "Second part.",
    ]
