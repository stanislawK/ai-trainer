"""The one chunker every reader feeds (ADR-0036): one word budget, small siblings merged, only
what is too large split, the abstract its own chunk, a table whole, references not embedded."""

from ai_trainer.domain.knowledge_document import (
    Block,
    BlockKind,
    KnowledgeDocument,
    NewKnowledgeChunk,
    PageLocator,
    SectionKind,
    SectionLocator,
    word_count,
)
from ai_trainer.knowledge.chunker import Chunker

_CHUNKER = Chunker(max_words=30, min_words=10)


def _words(n: int, word: str = "lift") -> str:
    return " ".join([word] * n)


def _sentences(n: int, words_each: int) -> str:
    return " ".join(f"{_words(words_each - 1)} end." for _ in range(n))


def _para(text: str, *path: str) -> Block:
    return Block(
        kind=BlockKind.PARAGRAPH,
        text=text,
        heading_path=path,
        locator=SectionLocator(section=path[-1] if path else None),
    )


def _heading(*path: str) -> Block:
    return Block(kind=BlockKind.HEADING, text=path[-1], heading_path=path)


def _chunk(*blocks: Block) -> tuple[NewKnowledgeChunk, ...]:
    return _CHUNKER.chunk(KnowledgeDocument(blocks=blocks))


def test_a_small_section_is_one_body_chunk_with_its_path_and_locator() -> None:
    chunks = _chunk(_heading("Deload"), _para(_words(12), "Deload"))

    assert chunks == (
        NewKnowledgeChunk(
            ordinal=0,
            text=_words(12),
            heading_path=("Deload",),
            section_kind=SectionKind.BODY,
            locator=SectionLocator(section="Deload"),
        ),
    )


def test_blocks_are_packed_up_to_the_budget() -> None:
    chunks = _chunk(*[_para(_words(12), "Deload") for _ in range(3)])

    assert [word_count(c.text) for c in chunks] == [24, 12]
    assert [c.ordinal for c in chunks] == [0, 1]
    assert chunks[0].text == f"{_words(12)}\n\n{_words(12)}"


def test_a_paragraph_over_the_budget_splits_on_sentence_ends() -> None:
    chunks = _chunk(_para(_sentences(5, 10), "Deload"))

    assert [word_count(c.text) for c in chunks] == [30, 20]
    assert all(c.text.endswith("end.") for c in chunks)


def test_a_sentence_over_the_budget_splits_on_words() -> None:
    chunks = _chunk(_para(_words(70), "Deload"))

    assert [word_count(c.text) for c in chunks] == [30, 30, 10]


def test_a_list_over_the_budget_splits_between_its_items_and_keeps_them_apart() -> None:
    items = [f"- {_words(12)} item{n}" for n in range(3)]  # 14 words each; two fit in 30
    block = Block(kind=BlockKind.LIST, text="\n".join(items), heading_path=("Plan",))

    chunks = _chunk(block)

    assert [c.text for c in chunks] == [f"{items[0]}\n{items[1]}", items[2]]


def test_a_table_stays_whole_with_its_caption_even_over_the_budget() -> None:
    table = Block(kind=BlockKind.TABLE, text="\n".join(["| a |"] * 40), heading_path=("Results",))
    caption = Block(kind=BlockKind.CAPTION, text="Table 1. Sets.", heading_path=("Results",))

    chunks = _chunk(_para(_words(12), "Results"), table, caption, _para(_words(12), "Results"))

    assert [c.section_kind for c in chunks] == [
        SectionKind.BODY,
        SectionKind.TABLE,
        SectionKind.BODY,
    ]
    assert chunks[1].text == f"{table.text}\n\n{caption.text}"


def test_the_abstract_is_its_own_chunk_and_never_merged() -> None:
    chunks = _chunk(
        _heading("Paper"),
        _heading("Paper", "Abstract"),
        _para(_words(3), "Paper", "Abstract"),
        _heading("Paper", "Intro"),
        _para(_words(3), "Paper", "Intro"),
    )

    assert [(c.section_kind, c.heading_path) for c in chunks] == [
        (SectionKind.ABSTRACT, ("Paper", "Abstract")),
        (SectionKind.BODY, ("Paper", "Intro")),
    ]


def test_a_polish_abstract_is_an_abstract() -> None:
    chunks = _chunk(_para(_words(12), "Streszczenie"))

    assert chunks[0].section_kind is SectionKind.ABSTRACT


def test_reference_lists_in_english_or_polish_are_not_embedded() -> None:
    chunks = _chunk(
        _para(_words(12), "Paper", "Results"),
        _para("Smith 2020. Jones 2021.", "Paper", "References"),
        _para("Kowalski 2019.", "Paper", "BIBLIOGRAFIA", "Książki"),
        _para("Nowak 2018.", "Piśmiennictwo"),
    )

    assert [c.heading_path for c in chunks] == [("Paper", "Results")]


def test_small_siblings_merge_under_their_parent_with_their_headings() -> None:
    chunks = _chunk(
        _heading("Deload"),
        _heading("Deload", "Timing"),
        _para(_words(5), "Deload", "Timing"),
        _heading("Deload", "Volume"),
        _para(_words(5), "Deload", "Volume"),
    )

    assert len(chunks) == 1
    assert chunks[0].heading_path == ("Deload",)
    assert chunks[0].text == f"Timing\n\n{_words(5)}\n\nVolume\n\n{_words(5)}"
    assert chunks[0].locator == SectionLocator(section="Timing")


def test_siblings_merge_only_up_to_the_budget() -> None:
    chunks = _chunk(*[_para(_words(9), "Deload", f"Part {n}") for n in range(4)])

    assert [c.heading_path for c in chunks] == [("Deload",), ("Deload",)]
    assert all(word_count(c.text) <= 30 for c in chunks)


def test_small_sections_under_different_parents_stay_apart() -> None:
    chunks = _chunk(_para(_words(5), "Squat", "Depth"), _para(_words(5), "Bench", "Grip"))

    assert [c.heading_path for c in chunks] == [("Squat", "Depth"), ("Bench", "Grip")]


def test_a_section_at_or_over_the_minimum_is_not_merged() -> None:
    chunks = _chunk(_para(_words(10), "Deload", "Timing"), _para(_words(5), "Deload", "Volume"))

    assert [c.heading_path for c in chunks] == [("Deload", "Timing"), ("Deload", "Volume")]


def _on_page(page: int, label: str | None = None) -> Block:
    return Block(
        kind=BlockKind.PARAGRAPH,
        text=_words(12),
        locator=PageLocator(page=page, page_label=label),
    )


def test_a_chunk_keeps_its_first_block_locator() -> None:
    chunks = _chunk(_on_page(3, "iii"), _on_page(3, "iii"))

    assert [c.locator for c in chunks] == [PageLocator(page=3, page_label="iii")]
    assert word_count(chunks[0].text) == 24


def test_a_chunk_never_spans_two_pages() -> None:
    chunks = _chunk(_on_page(3), _on_page(4))

    assert [(c.text, c.locator) for c in chunks] == [
        (_words(12), PageLocator(page=3)),
        (_words(12), PageLocator(page=4)),
    ]
    assert [c.ordinal for c in chunks] == [0, 1]


def test_a_document_of_only_references_gives_no_chunks() -> None:
    assert _chunk(_para("Smith 2020.", "References")) == ()
