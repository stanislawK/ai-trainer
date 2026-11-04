"""The Markdown reader builds ADR-0036's block model: headings into the heading path, paragraphs,
lists and pipe tables as blocks, front matter dropped, nothing inside a code fence read as a
heading (#423)."""

from ai_trainer.domain.knowledge_document import (
    Block,
    BlockKind,
    KnowledgeDocument,
    SectionLocator,
    Unreadable,
    UnreadableReason,
)
from ai_trainer.knowledge.markdown import read_markdown


def _blocks(source: str) -> list[tuple[BlockKind, str, tuple[str, ...]]]:
    document = read_markdown(source.encode())
    assert isinstance(document, KnowledgeDocument)
    return [(block.kind, block.text, block.heading_path) for block in document.blocks]


def test_headings_nest_into_the_heading_path() -> None:
    blocks = _blocks(
        "# Deload\n\nWhy rest.\n\n## Timing\n\nEvery fourth week.\n\n### Signs\n\nSleep.\n\n"
        "## Volume\n\nHalve it.\n"
    )

    assert blocks == [
        (BlockKind.HEADING, "Deload", ("Deload",)),
        (BlockKind.PARAGRAPH, "Why rest.", ("Deload",)),
        (BlockKind.HEADING, "Timing", ("Deload", "Timing")),
        (BlockKind.PARAGRAPH, "Every fourth week.", ("Deload", "Timing")),
        (BlockKind.HEADING, "Signs", ("Deload", "Timing", "Signs")),
        (BlockKind.PARAGRAPH, "Sleep.", ("Deload", "Timing", "Signs")),
        (BlockKind.HEADING, "Volume", ("Deload", "Volume")),
        (BlockKind.PARAGRAPH, "Halve it.", ("Deload", "Volume")),
    ]


def test_a_skipped_heading_level_still_nests_under_the_last_shallower_heading() -> None:
    blocks = _blocks("# Book\n\n### Deep\n\nText.\n\n## Middle\n\nMore.\n")

    assert [path for kind, _, path in blocks if kind is BlockKind.PARAGRAPH] == [
        ("Book", "Deep"),
        ("Book", "Middle"),
    ]


def test_closing_hashes_and_spacing_are_dropped_from_a_heading() -> None:
    assert _blocks("##   Squat depth  ##\n\nText.\n")[0] == (
        BlockKind.HEADING,
        "Squat depth",
        ("Squat depth",),
    )


def test_wrapped_paragraph_lines_join_into_one_block() -> None:
    assert _blocks("Rest   matters\nfor strength.\n") == [
        (BlockKind.PARAGRAPH, "Rest matters for strength.", ())
    ]


def test_a_list_keeps_its_lines() -> None:
    blocks = _blocks("Cues:\n\n- brace\n- breathe\n  out slowly\n1. lift\n\nAfter.\n")

    assert blocks == [
        (BlockKind.PARAGRAPH, "Cues:", ()),
        (BlockKind.LIST, "- brace\n- breathe\n  out slowly\n1. lift", ()),
        (BlockKind.PARAGRAPH, "After.", ()),
    ]


def test_a_pipe_table_is_one_block() -> None:
    blocks = _blocks("| Week | Sets |\n|---|---|\n| 1 | 3 |\n| 4 | 1 |\nAfter.\n")

    assert blocks == [
        (BlockKind.TABLE, "| Week | Sets |\n|---|---|\n| 1 | 3 |\n| 4 | 1 |", ()),
        (BlockKind.PARAGRAPH, "After.", ()),
    ]


def test_nothing_inside_a_code_fence_is_read_as_a_heading() -> None:
    blocks = _blocks("# Notes\n\n```\n# not a heading\nkeep\n```\n\nEnd.\n")

    assert blocks == [
        (BlockKind.HEADING, "Notes", ("Notes",)),
        (BlockKind.PARAGRAPH, "# not a heading\nkeep", ("Notes",)),
        (BlockKind.PARAGRAPH, "End.", ("Notes",)),
    ]


def test_an_unclosed_code_fence_runs_to_the_end() -> None:
    assert _blocks("~~~\ncode\n# still code\n") == [(BlockKind.PARAGRAPH, "code\n# still code", ())]


def test_front_matter_is_dropped() -> None:
    blocks = _blocks("---\ntitle: Deload\nlicense: CC BY 4.0\n---\n\n# Deload\n\nText.\n")

    assert blocks[0] == (BlockKind.HEADING, "Deload", ("Deload",))


def test_an_unclosed_front_matter_block_is_read_as_text() -> None:
    blocks = _blocks("---\nnot front matter\n")

    assert blocks == [(BlockKind.PARAGRAPH, "--- not front matter", ())]


def test_each_block_is_located_by_its_nearest_heading() -> None:
    document = read_markdown(b"Intro.\n\n# Deload\n\nText.\n")

    assert isinstance(document, KnowledgeDocument)
    assert [block.locator for block in document.blocks] == [
        SectionLocator(section=None),
        SectionLocator(section="Deload"),
        SectionLocator(section="Deload"),
    ]


def test_a_byte_order_mark_and_windows_line_ends_are_read() -> None:
    document = read_markdown("﻿# Deload\r\n\r\nText.\r\n".encode())

    assert document == KnowledgeDocument(
        blocks=(
            Block(
                kind=BlockKind.HEADING,
                text="Deload",
                heading_path=("Deload",),
                locator=SectionLocator(section="Deload"),
            ),
            Block(
                kind=BlockKind.PARAGRAPH,
                text="Text.",
                heading_path=("Deload",),
                locator=SectionLocator(section="Deload"),
            ),
        )
    )


def test_bytes_that_are_not_utf8_are_unreadable() -> None:
    assert read_markdown(b"\xff\xfe# x") == Unreadable(reason=UnreadableReason.NOT_UTF8)


def test_a_file_with_only_headings_has_no_text() -> None:
    assert read_markdown(b"# Title\n\n## Empty\n") == Unreadable(reason=UnreadableReason.NO_TEXT)
